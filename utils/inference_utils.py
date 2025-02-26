#Built-in packages
import time
import random
from collections import defaultdict
import os
from tqdm import tqdm
from string import ascii_lowercase, ascii_uppercase
# External packages
import numpy as np
import pandas as pd
import torch
import openai
import backoff  # for exponential backoff
# Local packages
from utils.response_utils import HF_RESPONSE, get_explanation_probs, parse_response, compute_ece, get_correct, get_true_labels, get_random_acc
from utils.question_utils import reconstruct_context, build_prefix, get_test_questions, permute_answer, convert_probabilities, append_question
from utils.parsing_utils import find_answer_letter
from utils.utils import get_option_letters, print_chat, read_as_defaultdict, load_dfs, get_chat_type, ParameterGrid
from utils.model_utils import GPTClient, MODEL_PATH, load_model_tokenizer
from utils.logger_utils import JobLogger
from utils.dataset_utils import load_results, load_metadata, check_num_rows, get_uncompleted_dfs
import torch

QUESTIONS_DF, QUESTIONS_METADATA, OPTIONS_DF, ANSWERS_DF = None, None, None, None

OPTIONS = list(ascii_uppercase)
LETTERS = list(ascii_lowercase)

###########################################
##                                       ##
##             Helper Code               ##
##                                       ##
###########################################


def exponential_backoff_decorator(max_retries, base_delay):
    def decorator(func):
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    result_func = func(*args, **kwargs)
                    return result_func
                except Exception as e:
                    print(f"Attempt {retries + 1} failed: {e}")
                    retries += 1
                    delay = (base_delay * 2 ** retries + random.uniform(0, 1))
                    print(f"Retrying in {delay:.2f} seconds...")
                    time.sleep(delay)
            # raise Exception("Max retries reached, operation failed.")
            print("Max retries reached, operation failed.")

        return wrapper

    return decorator

@backoff.on_exception(backoff.expo, openai.RateLimitError)
def get_response(client, model, prefix, questions, question_type, options_lst, chat_type):
    outputs =  []
    parsed_results = []

    for i, question in enumerate(questions):
        context = reconstruct_context(prefix, questions[:i], outputs, chat_type)

        if len(context) > 1:
            context.append({'role': 'user', 'content': question})
        else:
            context[0]['content'] += '\n' + question

        # NOTE: until we get access to tokenizer can't to mc-separte
        if question_type == 'mc':
            answer, probs = client.get_answer(valid_tokens = get_option_letters(options_lst)[i], model=model, messages = context, max_tokens = 1, logprobs = True, top_logprobs=len(options_lst[i])*2)
            outputs.append(answer)
            parsed_results.append(['', answer, probs])
        
        # Model is allowed to explain and answer
        elif question_type == 'explanation':
            output = client.get_explanation(model = model, messages = context, max_tokens = None)
            explanation, answer = parse_response(output, options_lst, i)
            outputs.append(output)
            parsed_results.append([explanation, answer, defaultdict(lambda: 0)])
            # TODO: parse output

        # Model is allowed to explain only
        elif i % 2 == 0 and (question_type == 'sequential-hidden' or question_type == 'sequential-shown'):
            output = client.get_explanation(model = model, messages = context, max_tokens = None)
            outputs.append(output)

        # Model is allowed to answer only
        elif i % 2 == 1 and (question_type == 'sequential-hidden' or question_type == 'sequential-shown'):
            answer, probs = client.get_answer(valid_tokens = get_option_letters(options_lst)[i//2], model=model, messages = context, max_tokens = 1, logprobs = True, top_logprobs=len(options_lst[i//2])*2)
            outputs.append(answer)
            parsed_results.append([outputs[i-1], answer, probs])
    
    return np.array(parsed_results).T.tolist()

def get_response_hf(model, tokenizer, device, prefix, questions, question_type, options_lst, chat_type):
    outputs = []
    parsed_results = []

    for i, question in enumerate(questions):
        # context is either a list of dictionaries or a string depending on chat_type
        context = reconstruct_context(prefix, questions[:i], outputs, chat_type)
        
        prompt = append_question(context, question, chat_type)

        if question_type == 'mc' or question_type == 'mc-separate':
            answer, probs = HF_RESPONSE[question_type](model, tokenizer, prompt, options_lst[i], device, chat_type)
            outputs.append(answer)
            parsed_results.append(['', answer, probs])

        elif question_type == 'explanation':
            output = HF_RESPONSE[question_type](model, tokenizer, prompt, device, chat_type)
            outputs.append(output)
            answer_letter = find_answer_letter(output)
            
            new_output, probs = get_explanation_probs(model, tokenizer, context, output, options_lst[i], device)

            # TODO: should check if new_output is the same as answer_letter

            parsed_results.append([output, answer_letter, probs])
        
        elif i % 2 == 0 and (question_type == 'sequential-hidden' or question_type == 'sequential-shown'):
            output = HF_RESPONSE['explanation'](model, tokenizer, prompt, device, chat_type)
            outputs.append(output)

        elif i % 2 == 1 and (question_type == 'sequential-hidden' or question_type == 'sequential-shown'):
            answer, probs = HF_RESPONSE['mc'](model, tokenizer, prompt, options_lst[i], device, chat_type)
            outputs.append(answer)
            parsed_results.append([outputs[i-1], answer, probs])
        

        # TODO: add condition where the model is asked if the answer is correct or not
    return np.array(parsed_results, dtype=object).T.tolist()


#########################################################################################
#########################################################################################
###                                                                                   ###
###                                                                                   ###
###                                                                                   ###
###                             Run Inference Code                                    ###
###                                                                                   ###
###                                                                                   ###
###                                                                                   ###
#########################################################################################
#########################################################################################





def create_results_dict(params, task_name, model_name, base_id, sub_id, permuted_answer, model_answer, model_explanation, probabilities, permutations):
    # Setup results_dict
    results = params
    results['task_name'] = task_name
    results['model'] = model_name
    results['question_id'] = f'{base_id}_{sub_id}'
    results['permuted_answer'] = permuted_answer
    results['model_answer'] = model_answer
    results['model_explanation'] = model_explanation
    results['probabilities'] = probabilities
    results['accuracy'] = get_correct(results['question_id'], ANSWERS_DF, permuted_answer)
    # TODO: when expanding to multiple part questions, need to update get_random_acc to take in task_name and questions_metadata
    results['normalized_accuracy'] = results['accuracy'] - get_random_acc(results['question_id'], ANSWERS_DF)
    
    true_labels = get_true_labels(results['question_id'], ANSWERS_DF)
    probabilities_list = convert_probabilities(probabilities, sub_id, permutations)
    results['expected_calibration'] = compute_ece(np.array(probabilities_list), np.array(true_labels))
    
    return results


def eval_models(args, api, device=None):

    model_names = list(args['models'].keys())
    # save results per model
    for model_name in model_names:
        if api:
            job_logger = JobLogger(f"logs/{args['task_name']}/{model_name}/")
            progress_bar = job_logger.tqdm
        else:
            job_logger = None
            progress_bar = tqdm
        
        results_path = os.path.join(args['output_path'], model_name + '.pkl')
        metadata_path = os.path.join(args['output_path'], model_name + '_metadata.pkl')

        results_df = load_results(results_path)
        if check_num_rows(results_df, args):
            print(f"Model {model_name} has already been evaluated.")
            continue
        results_metadata = load_metadata(metadata_path)
        
        # Load model and tokenizer
        if device:
            num_gpus = torch.cuda.device_count()

            print("Loading model:", model_name)
            model, tokenizer = load_model_tokenizer(os.path.join(MODEL_PATH, model_name), device, num_gpus, )
            if not model:
                continue
            else:
                print(f'Model {model_name} loaded')
        else:
            client = GPTClient()
        

        # Setup parameter grid
        param_grid = ParameterGrid([
            {
                'num_shots': [
                    0,
                    # 1, 
                    # 2, 
                    # 5
                ], 
                'allow_explanation': [False], 
                'question_type': [
                    'mc',
                    'mc-separate'
                ], 
                'num_sample': [args['num_sample']]
            }, 
            # {
            #     'num_shots': [
            #         0, 
            #         1, 
            #         2, 
            #         5
            #     ], 
            #     'allow_explanation': [True], 
            #     'question_type': [
            #         'explanation',
            #         'sequential-hidden', 
            #         'sequential-shown'
            #     ], 
            #     'num_sample': [args['num_sample']]
            # }
            ])
        
        questions_df, questions_metadata, options_df, answers_df = get_uncompleted_dfs([QUESTIONS_DF, QUESTIONS_METADATA, OPTIONS_DF, ANSWERS_DF], results_df['question_id'].tolist())
        
        # Running inference
        for params in param_grid:
            test_metadata = questions_metadata.iloc[questions_df.query("explanation == False").index]
            sampled_df = test_metadata.groupby(['type', 'domain', 'difficulty_level']).sample(n=params['num_sample'], random_state=42)
            sampled_qids = set(sampled_df['question_id'])

            # Filtered DataFrames based on sampled question_ids
            filtered_questions_df = questions_df.query("question_id in @sampled_qids")
            filtered_options_df = options_df.query("question_id in @sampled_qids")

            base_ids = set(question_id.split('_')[0] for question_id in sampled_qids)

            # iterate over questions by id
            for run_num, base_id in enumerate(progress_bar(base_ids, desc=str(params), dynamic_ncols=True)):
                # build prefix for few-shot prompting
                task_data = questions_metadata.query(f"question_id == '{base_id}_0'").to_dict('records')[0]
                prefix = build_prefix(task_data, questions_df, options_df, questions_metadata, answers_df, params)

                # build question string
                test_questions, test_options, permutations = get_test_questions(base_id, filtered_questions_df, filtered_options_df, params)

                # Track total time to run inference on a model
                start_time = time.time()

                # Get model answer for question
                if device:
                    model_explanations, model_answers, probabilities = get_response_hf(
                        model=model, 
                        tokenizer=tokenizer, 
                        device=device, 
                        prefix=prefix, 
                        questions=test_questions, 
                        question_type=params['question_type'],
                        options_lst=test_options,
                        chat_type=get_chat_type(model_name)
                    )
                else:
                    try:
                        model_explanations, model_answers, probabilities = get_response(
                            client=client, 
                            model=model_name,
                            prefix=prefix, 
                            questions=test_questions, 
                            question_type=params['question_type'],
                            options_lst=test_options,
                            chat_type=get_chat_type(model_name)
                        )
                    except openai.BadRequestError as e:
                        print(f"Error: {e}")
                        continue


                inference_time = time.time() - start_time

                # Reverse permutation answer
                # Permuted answers are the index value into the list of options
                permuted_answers = permute_answer(model_answers=model_answers, permutations=permutations)

                # Instantiate the results dataframe: num_shots, allow_explanation, etc.
                result_params = params.copy()
                # This is domain, type, difficulty_level
                for data in task_data:
                    result_params[data] = task_data[data]
                result_params.pop('num_sample')
                # Store results
                results = [create_results_dict(
                    params = result_params,
                    task_name = args['task_name'],
                    model_name = model_name,
                    base_id = base_id,
                    sub_id = i,
                    permuted_answer = permuted_answer,
                    model_answer = model_answers[i],
                    model_explanation = model_explanations[i],
                    probabilities = probabilities[i],
                    permutations = permutations
                ) for i, permuted_answer in enumerate(permuted_answers)]
                
                results_df = pd.concat([results_df, pd.DataFrame.from_records(results)], sort=False, ignore_index=True)
                
                result_metadata = [{
                    'task_name': args['task_name'],
                    'model_name': model_name,
                    'question_id': f"{base_id}_{i}",
                    'permutation': permutation,
                    'prompt': test_questions,
                    'inference_time': inference_time
                    } for i, permutation in enumerate(permutations)]
                results_metadata = pd.concat([results_metadata, pd.DataFrame.from_records(result_metadata)], sort=False, ignore_index=True)

                if run_num % 10 == 0 and job_logger is not None:
                    job_logger.log_groupby_counts(results_df, ['domain', 'difficulty_level', 'type', 'num_shots', 'allow_explanation'])
        
        # Save per model
        if not os.path.exists(args['output_path']):
            os.mkdir(args['output_path'])
        results_df.to_pickle(results_path)
        results_metadata.to_pickle(metadata_path)


def run_evaluation(input_path: str, api: bool):
    args = read_as_defaultdict(input_path)

    global QUESTIONS_DF, QUESTIONS_METADATA, OPTIONS_DF, ANSWERS_DF
    QUESTIONS_DF, QUESTIONS_METADATA, OPTIONS_DF, ANSWERS_DF = load_dfs(args['task_path'])


    if api:
        eval_models(args, api)
    else:
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        print('device:', device)
        with torch.inference_mode():
            eval_models(args, False, device)


def dir_path(string):
    if os.path.isdir(string):
        return string
    else:
        raise NotADirectoryError(string)