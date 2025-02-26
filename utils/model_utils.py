# Built-in packages
import os
from typing import Optional
from collections import defaultdict
from math import exp
# External packages
from openai import OpenAI, AzureOpenAI
from transformers import AutoTokenizer, AutoModel, AutoModelForCausalLM
import torch
from accelerate import Accelerator
# Local packages
from utils.utils import get_gpu_memory, normalize_dict

class GPTClient:
    def __init__(self, client='azure'):
        if client.lower() == 'azure':
            self.client = AzureOpenAI(
                api_key=os.environ['AZURE_OPENAI_API_KEY'],  
                api_version="2024-02-01",
                azure_endpoint = os.environ['AZURE_OPENAI_ENDPOINT']
            )
        elif client.lower() == 'openai':
            self.client = OpenAI(
                api_key=os.environ['OPENAI_API_KEY']
            )
    
    def get_completion(
        self,
        messages: list[dict[str, str]],
        model: str = "gpt-4-1106-preview",
        max_tokens=500,
        temperature=0,
        stop=None,
        seed=123,
        tools=None,
        logprobs=None,  # whether to return log probabilities of the output tokens or not. If true, returns the log probabilities of each output token returned in the content of message..
        top_logprobs=None,
    ) -> str:
        params = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stop": stop,
            "seed": seed,
            "logprobs": logprobs,
            "top_logprobs": top_logprobs,
        }
        if tools:
            params["tools"] = tools

        completion = self.client.chat.completions.create(**params)
        return completion

    def get_explanation(
        self,
        messages: list[dict[str, str]],
        model: str = 'gpt-4',
        max_tokens = 500,
        temperature = 0,
        stop = None,
        seed = 123, 
        tools = None,
        logprobs = None,
        top_logprobs=None,
    ) -> str:
        response = self.get_completion(
            messages,
            model,
            max_tokens,
            temperature,
            stop,
            seed,
            tools,
            logprobs,
            top_logprobs
        )
        return response.choices[0].message.content
    
    def get_answer(
        self,
        valid_tokens,
        messages: list[dict[str, str]],
        model: str = 'gpt-4',
        max_tokens = 500,
        temperature = 0,
        stop = None,
        seed = 123, 
        tools = None,
        logprobs = None,
        top_logprobs=None,
    ) -> dict:
        response = self.get_completion(
            messages,
            model,
            max_tokens,
            temperature,
            stop,
            seed,
            tools,
            logprobs,
            min(5, top_logprobs) # Azure currently only supports top 5 logprobs
        )
        top_responses = response.choices[0].logprobs.content[0].top_logprobs
        output = defaultdict(lambda: 0)
        for logprob in top_responses:
            for valid_token in valid_tokens:
                if valid_token.startswith(logprob.token.upper()):
                    output[logprob.token] = exp(logprob.logprob)*100
            if len(output) == len(valid_tokens):
                break
        output = normalize_dict(output)
        return max(output, key=output.get), output 


    

#########################################################################################
#########################################################################################
##                                                                                     ##
##                                                                                     ##
##                                                                                     ##
##                               Model Helper Code                                     ##
##                                                                                     ##
##                                                                                     ##
##                                                                                     ##
#########################################################################################
#########################################################################################

MODEL_PATH = '/home/narunram/scratch/models/'


def load_model(base_path: str, from_pretrained_kwargs: dict):
    if not os.path.exists(base_path):
        print(f'Skipping model {base_path}. Cannot be found in {base_path}.')
        return False, False
    tokenizer = AutoTokenizer.from_pretrained(base_path, local_files_only=True)
    try:
        model = AutoModelForCausalLM.from_pretrained(base_path, **from_pretrained_kwargs, local_files_only=True, trust_remote_code=True)
    except NameError:
        model = AutoModel.from_pretrained(base_path, low_cpu_mem_usage=True, **from_pretrained_kwargs, local_files_only=True, trust_remote_code=True)
    return model, tokenizer


def build_kwargs(device: str, num_gpus: int, max_gpu_mem: Optional[str] = None):
    if device == "cpu":
        kwargs = {"torch_dtype": torch.float32}
    elif device == "cuda":
        kwargs = {"torch_dtype": torch.float16}
        if num_gpus != 1:
            kwargs["device_map"] = "auto"
            if max_gpu_mem is None:
                kwargs["device_map"] = "sequential"  # This is important for not the same VRAM sizes
                available_gpu_memory = get_gpu_memory(num_gpus)
                kwargs["max_memory"] = {i: str(int(available_gpu_memory[i] * 0.85)) + "GiB" for i in range(num_gpus)}
            else:
                kwargs["max_memory"] = {i: max_gpu_mem for i in range(num_gpus)}
    else:
        raise ValueError(f"Invalid device: {device}")

    return kwargs

def load_model_tokenizer(model_path: str, device: str = "cuda", num_gpus: int = 2, max_gpu_mem: Optional[str] = None):
    kwargs = build_kwargs(device, num_gpus, max_gpu_mem)
    
    model_path = os.path.join(model_path, 'snapshots/')
    model_path = os.path.join(model_path, os.listdir(model_path)[0])
    print('Loading model from:', model_path) 
    try:
        model, tokenizer = load_model(model_path, kwargs)
    except Exception as error:
        print(f'Error loading model: {error}')
        return False, False


    if (device == "cuda" and num_gpus == 1) and model != False:
        model.to(device)

    return model, tokenizer