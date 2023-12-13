import json
import torch
import gc
import argparse
import os
import sys
import pandas as pd
from transformers import LlamaTokenizerFast, LlamaForCausalLM
from vllm import LLM, SamplingParams
import ray

def pad(list, n):
    return list + [0.] * (n - len(list))

def prepare_for_arrow(tensor):
    return tensor.cpu().tolist()

def collect_text_instruct_coder(dataset_item):
    return f"{dataset_item['instruction']} {dataset_item['input']} {dataset_item['output']}"

def collect_text_sharegpt(dataset_item):
    acc = ''
    state = 0
    for c in dataset_item['conversations']:
        if state == 0 and c['from'] == 'human':
            acc = acc + ' ' + c['value']
            state = 1
        if state == 1 and c['from'] == 'gpt':
            acc = acc + ' ' + c['value']
            state = 2
        if state == 2 and c['from'] == 'human':
            acc = acc + ' ' + c['value']
            break
    return acc

collect_text = {
    "sharegpt": collect_text_sharegpt,
    "instruct_coder": collect_text_instruct_coder,
}

@torch.no_grad()
def main(args):
    dataset_in = args.dataset_in
    dataset_out = args.dataset_out

    print("Loading dataset...")
    with open(dataset_in, "r") as f:
        dataset = json.load(f)

    print("Loading models...")
    tokenizer = LlamaTokenizerFast.from_pretrained(args.main_model)
    
    main_model_generate = ray.remote(num_gpus=(1 if args.vllm_tp == 1 else 0))(LLM).remote(
        model=args.main_model,
        tensor_parallel_size=args.vllm_tp)
    main_model_generate_sp = SamplingParams(
        temperature=0.0,
        max_tokens=args.generation_tokens,
    )

    main_model = LlamaForCausalLM.from_pretrained(args.main_model, use_cache=False, torch_dtype=torch.float16, device_map="auto")
    draft_model = LlamaForCausalLM.from_pretrained(args.draft_model, use_cache=False, torch_dtype=torch.float16)
    main_model.half()
    # main_model.cuda()
    draft_model.cuda()

    missed_tokens = 0

    print("Processing dataset...")
    dataset_processed = []
    dataset_index = args.offset

    def save():
        nonlocal dataset_processed
        nonlocal dataset_out
        # save as dataframe
        print(f"saving dataset to {dataset_out}")
        df = pd.DataFrame(
            dataset_processed, 
            columns=["input_ids", "main_hidden_states", "draft_hidden_states", "accept_mask", "dataset_index"])
        # save as parquet
        # append to the existing dataset
        if os.path.exists(dataset_out):
            df.to_parquet(dataset_out, engine='fastparquet', append=True)
        else:
            df.to_parquet(dataset_out, engine='fastparquet')
        dataset_processed = []

    total_processed = 0

    while total_processed < args.n:
        batch = []
        while len(batch) < args.batch_size:
            d = dataset[dataset_index]
            dataset_index += 1
            prompt = collect_text[args.dataset_type](d)
            input_ids = tokenizer.encode(prompt, return_tensors="pt").squeeze(0)
            if input_ids.shape[0] > args.prompt_tokens:
                input_ids = input_ids[:args.prompt_tokens]
            else:
                # ensure that the context is not too short
                continue
            bi = {
                "input_ids": input_ids,
                "index": dataset_index - 1,
            }
            batch.append(bi)
        batch_input_ids_l = [b['input_ids'].tolist() for b in batch]
        batch_input_ids_t = torch.tensor(batch_input_ids_l, dtype=torch.int64).cuda()
        # generate completions
        out_generate_r = main_model_generate.generate.remote(
            prompt_token_ids=batch_input_ids_l, 
            sampling_params=main_model_generate_sp, 
            use_tqdm=False
        )
        out_generate = ray.get(out_generate_r)
        batch_output_ids_l = [pad(out.outputs[0].token_ids, args.prompt_tokens) for out in out_generate]
        batch_output_ids_t = torch.tensor(batch_output_ids_l, dtype=torch.int64).cuda()
        all_ids = torch.cat([batch_input_ids_t, batch_output_ids_t], dim=1)
        # run a normal forward pass to get hidden states
        out_main_encode = main_model.forward(
            all_ids,
            output_hidden_states=True,
            use_cache=False,
        )
        # get hidden states
        main_hidden_states = out_main_encode.hidden_states[-1]
        main_hidden_states = main_hidden_states[:, args.prompt_tokens-1:-1, :]
        
        out_draft_encode = draft_model.forward(
            all_ids,
            output_hidden_states=True,
            use_cache=False,
        )
        draft_hidden_states = out_draft_encode.hidden_states[-1]
        draft_hidden_states = draft_hidden_states[:, args.prompt_tokens-1:-1, :]

        draft_logits = draft_model.get_output_embeddings().forward(draft_hidden_states)
        draft_prediction = torch.argmax(draft_logits, dim=-1).to(batch_output_ids_t.device)
        # get mask of tokens where the draft model was incorrect
        mask = torch.where(draft_prediction != batch_output_ids_t, 1, 0)

        valid = 0
        for i in range(args.batch_size):
            # drop it if not the max length
            if out_generate[i].outputs[0].finish_reason == "length":
                missed_tokens += mask[i].sum().item()
                item = {
                    "input_ids": prepare_for_arrow(batch_input_ids_t[i]),
                    "main_hidden_states": prepare_for_arrow(main_hidden_states[i]),
                    "draft_hidden_states": prepare_for_arrow(draft_hidden_states[i]),
                    "accept_mask": prepare_for_arrow(mask[i]),
                    "dataset_index": batch[i]['index'],
                }
                dataset_processed.append(item)
                total_processed += 1
                valid += 1
                
                if total_processed % args.writeback_interval == 0 and total_processed > 0:
                    save()
        
        gc.collect()

        print(f"added {valid} items")

    total_generated = len(dataset_processed) * args.generation_tokens

    

    print("Total generated tokens:", total_generated)
    print("Total accepted tokens:", total_generated - missed_tokens)
    print("Acceptance rate:", (total_generated - missed_tokens) / total_generated)

    save()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_in", type=str, help="Path to dataset")
    parser.add_argument("dataset_out", type=str, help="Path to processed dataset")
    parser.add_argument("--prompt_tokens", type=int, default=256)
    parser.add_argument("--generation_tokens", type=int, default=256)
    parser.add_argument("--main_model", type=str, default="JackFram/llama-160m")
    parser.add_argument("--draft_model", type=str, default="JackFram/llama-160m")
    parser.add_argument("--n", type=int, default=10000)
    parser.add_argument("--batch_size", type=int, default=16)
    parser.add_argument("--writeback_interval", type=int, default=1000)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--dataset_type", type=str, default="sharegpt")
    parser.add_argument("--vllm_tp", type=int, default=4)
    parser.add_argument("--generation_workers", type=int, default=1)
    args = parser.parse_args()
    
    runtime_env = {
        "env_vars": {"HUGGING_FACE_HUB_TOKEN": os.getenv("HUGGING_FACE_HUB_TOKEN")}
    }
    ray.init(runtime_env=runtime_env)

    main(args)
