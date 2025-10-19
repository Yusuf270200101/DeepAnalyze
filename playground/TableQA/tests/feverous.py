import json
import os
import logging
import time
import re
import sys
import argparse
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional
from vllm import LLM, SamplingParams
from transformers import AutoTokenizer
from tqdm import tqdm
from prompt import COT_PROMPT_FEVEROUS_TEMPLATE


# Setup logging
def setup_logger(log_file):
    """Set up the logger"""
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logger = logging.getLogger("feverous_processor")
    logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


class VLLMGenerator:
    """
    A class for generating text using vLLM with support for different models.
    """

    def __init__(self, model_path, max_model_len=16384, tensor_parallel_size=1):
        """
        Initialize the VLLMGenerator with model and tokenizer.
        """
        # Default EOS tokens list - can be overridden based on model
        self.EOS = ["<|im_end|>", "</s>"]

        self.model = LLM(
            model=model_path,
            max_model_len=max_model_len,
            trust_remote_code=True,
            distributed_executor_backend="mp",
            tensor_parallel_size=tensor_parallel_size,
        )

        self.tokenizer = AutoTokenizer.from_pretrained(model_path)

    def generate(
        self, prompts, max_new_tokens=2048, temperature=0.0, top_p=1.0, verbose=False
    ):
        try:
            # Apply chat template to prompts
            chat_prompts = []
            for prompt in prompts:
                # Convert to chat format with a single user message
                messages = [{"role": "user", "content": prompt}]
                chat_prompts.append(
                    self.tokenizer.apply_chat_template(
                        messages,
                        tokenize=False,
                        add_generation_prompt=False,
                    )
                )

            if verbose and len(chat_prompts) > 0:
                print("Example chat prompt:")
                print(chat_prompts[0])

            # Batch generation with vLLM
            vllm_outputs = self.model.generate(
                prompts=chat_prompts,
                sampling_params=SamplingParams(
                    max_tokens=max_new_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    stop=self.EOS,
                ),
                use_tqdm=True,
            )

            # Process generated outputs
            raw_generations = [x.outputs[0].text for x in vllm_outputs]
            return raw_generations

        except Exception as e:
            print(f"Error in vLLM generation: {str(e)}")
            raise


def format_table(table_data):
    """Format table data into string representation"""
    if not table_data:
        return ""

    result = ""
    for table_id, table_content in table_data.items():
        result += f"Table {table_id}:\n"
        table = table_content.get("table", [])
        for row in table:
            result += " | ".join([str(cell) for cell in row]) + "\n"
        result += "\n"

    return result.strip()


def format_sentences(sentences_data):
    """Format sentence data into string representation"""
    if not sentences_data:
        return ""

    result = "Text excerpts:\n"
    for key, sentence in sentences_data.items():
        result += f"- {sentence}\n"

    return result


def extract_answer_from_response(model_answer: str) -> Optional[str]:
    """Extract final answer from model response, supporting multiple formats"""
    if not model_answer:
        return None

    # Try to extract answer from <answer> tags
    answer_tag_pattern = re.search(r"<Answer>(.*?)</Answer>", model_answer, re.DOTALL)
    if answer_tag_pattern:
        answer_content = answer_tag_pattern.group(1).strip()

        # Check if there's an "Answer:" prefix inside the tags
        if "Answer:" in answer_content:
            after_answer = answer_content.split("Answer:", 1)[1].strip()
        else:
            after_answer = answer_content

        # Check for label in the extracted content
        if re.search(
            r"(SUPPORTS|REFUTES|NOT ENOUGH INFO)", after_answer, re.IGNORECASE
        ):
            answer_text = after_answer.upper()
            if "SUPPORTS" in answer_text:
                return "SUPPORTS"
            elif "REFUTES" in answer_text:
                return "REFUTES"
            elif "NOT ENOUGH INFO" in answer_text:
                return "NOT ENOUGH INFO"

    # Try to use regex to match "Answer: xxx" pattern
    match = re.search(
        r"Answer:\s*(SUPPORTS|REFUTES|NOT ENOUGH INFO|supports|refutes|not enough info)",
        model_answer,
        re.IGNORECASE | re.MULTILINE,
    )
    if match:
        answer_text = match.group(1).strip().upper()
        # Normalize answer format
        if "SUPPORTS" in answer_text:
            return "SUPPORTS"
        elif "REFUTES" in answer_text:
            return "REFUTES"
        elif "NOT ENOUGH INFO" in answer_text:
            return "NOT ENOUGH INFO"

    # If "Answer:" exists anywhere in the text, extract everything after it
    if "Answer:" in model_answer:
        after_answer = model_answer.split("Answer:", 1)[
            1
        ].strip()  # Split only on first occurrence

        # Check if the extracted part has any of the expected labels
        if re.search(
            r"(SUPPORTS|REFUTES|NOT ENOUGH INFO)", after_answer, re.IGNORECASE
        ):
            answer_text = after_answer.upper()
            if "SUPPORTS" in answer_text:
                return "SUPPORTS"
            elif "REFUTES" in answer_text:
                return "REFUTES"
            elif "NOT ENOUGH INFO" in answer_text:
                return "NOT ENOUGH INFO"

    # If no explicit Answer tag, try to find in the last few lines
    last_lines = model_answer.strip().split("\n")[-3:]  # Get last three lines
    for line in reversed(last_lines):
        if "SUPPORTS" in line.upper():
            return "SUPPORTS"
        elif "REFUTES" in line.upper():
            return "REFUTES"
        elif "NOT ENOUGH INFO" in line.upper():
            return "NOT ENOUGH INFO"

    # If all extraction methods fail, return None
    return None


def create_prompt_from_feverous(item: Dict[str, Any]) -> str:
    """Create prompt for FEVEROUS item"""
    # Get claim
    claim = item.get("claim", "")

    # Get evidence content
    evidence_content = item.get("evidence_content", {})

    # Format tables
    tables = evidence_content.get("tables", {})
    table_str = format_table(tables)

    # Format text
    sentences = evidence_content.get("sentences", {})
    text_str = format_sentences(sentences)

    # Create prompt
    prompt = COT_PROMPT_FEVEROUS_TEMPLATE.format(
        table=table_str, text=text_str, claim=claim
    )

    return prompt


def process_feverous_data_batch(
    input_file,
    output_file,
    model_path,
    log_file,
    max_tokens=2048,
    temperature=0.0,
    tensor_parallel_size=1,
    batch_size=16,
    start_from=0,
):
    """Process FEVEROUS dataset with batched VLLM inference"""
    logger = setup_logger(log_file)

    # Record start time
    start_time = time.time()
    logger.info(
        f"Started processing FEVEROUS data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    logger.info(f"Input file: {input_file}")
    logger.info(f"Output file: {output_file}")
    logger.info(f"Using model: {model_path}")
    logger.info(f"Batch size: {batch_size}")

    # Initialize VLLM generator
    try:
        generator = VLLMGenerator(
            model_path=model_path,
            max_model_len=16384,
            tensor_parallel_size=tensor_parallel_size,
        )
        logger.info(f"VLLM generator initialized successfully")
    except Exception as e:
        logger.error(f"VLLM initialization failed: {e}")
        return

    # Read data items
    data_items = []
    try:
        # For JSONL format, read line by line
        with open(input_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():  # Skip empty lines
                    item = json.loads(line.strip())
                    data_items.append(item)
        logger.info(f"Loaded {len(data_items)} data items from JSONL file")
    except Exception as e:
        logger.error(f"Failed to read input file: {e}")
        return

    # Check if intermediate results exist
    results = []
    processed_ids = set()
    if os.path.exists(f"{output_file}.temp"):
        try:
            with open(f"{output_file}.temp", "r", encoding="utf-8") as f:
                results_data = json.load(f)
                # Check if results are in nested format or flat format
                if "results" in results_data:
                    results = results_data["results"]
                else:
                    results = results_data

            # Get IDs of already processed items
            for result in results:
                processed_ids.add(result.get("id", ""))

            logger.info(f"Loaded intermediate results with {len(results)} records")
            logger.info(f"Found {len(processed_ids)} already processed items")
        except Exception as e:
            logger.error(
                f"Failed to load intermediate results: {e}, starting from beginning"
            )
            results = []
            processed_ids = set()

    # Filter out already processed items and apply start_from
    remaining_items = []
    for idx, item in enumerate(data_items):
        if idx < start_from:
            continue
        item_id = item.get("id", f"item_{idx}")
        if item_id not in processed_ids:
            remaining_items.append(item)

    logger.info(f"Remaining items to process: {len(remaining_items)}/{len(data_items)}")

    success_count = len(results)
    error_count = 0
    correct_count = sum(1 for result in results if result.get("is_correct", False))

    # Process data in batches
    for batch_start in range(0, len(remaining_items), batch_size):
        batch_end = min(batch_start + batch_size, len(remaining_items))
        current_batch = remaining_items[batch_start:batch_end]

        # Create prompts for current batch
        prompts = []
        for item in current_batch:
            prompt = create_prompt_from_feverous(item)
            prompts.append(prompt)

        # Generate responses for the batch
        try:
            responses = generator.generate(
                prompts=prompts,
                max_new_tokens=max_tokens,
                temperature=temperature,
                top_p=0.8,
                verbose=(batch_start == 0),  # Only show example on first batch
            )

            # Process each response
            for i, (item, response) in enumerate(zip(current_batch, responses)):
                # Record start time for processing this item
                item_start_time = time.time()

                # Extract item information
                item_id = item.get("id", f"item-{i+batch_start}")
                claim = item.get("claim", "")
                expected_label = item.get("label", "")

                # Calculate current item index in overall dataset
                global_item_index = batch_start + i + 1

                logger.info(
                    f"Processing item {global_item_index}/{len(remaining_items)}... [ID: {item_id}]"
                )

                # Extract final answer
                extracted_answer = extract_answer_from_response(response)
                if not extracted_answer:
                    logger.warning(
                        f"Unable to extract label from answer, using 'NOT_EXTRACTED'"
                    )
                    extracted_answer = "NOT_EXTRACTED"

                # Check if prediction is correct
                is_correct = extracted_answer == expected_label
                if is_correct:
                    correct_count += 1

                # Calculate processing time
                item_time = time.time() - item_start_time

                # Log detailed information
                logger.info(f"Claim: {claim}")
                logger.info(f"Expected label: {expected_label}")
                logger.info(f"Model prediction: {extracted_answer}")
                logger.info(f"Is correct: {is_correct}")
                logger.info(f"Processing time: {item_time:.6f} seconds")
                logger.info("-" * 50)

                # Build result object
                result = {
                    "id": item_id,
                    "claim": claim,
                    "prompt": prompts[i],
                    "ground_truth": expected_label,
                    "model_prediction": extracted_answer,
                    "full_response": response,
                    "is_correct": is_correct,
                    "processing_time": item_time,
                }

                results.append(result)
                success_count += 1

                # Save intermediate results every 5 items
                if global_item_index % 500 == 0:
                    # Add evaluation metrics to the intermediate results
                    accuracy = (
                        correct_count / (success_count + error_count)
                        if (success_count + error_count) > 0
                        else 0
                    )
                    final_results = {
                        "results": results,
                        "metadata": {
                            "total_examples": len(data_items),
                            "processed_examples": len(results),
                            "correct_predictions": correct_count,
                            "accuracy": accuracy,
                            "model": model_path,
                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        },
                    }

                    with open(f"{output_file}.temp", "w", encoding="utf-8") as f:
                        json.dump(final_results, f, ensure_ascii=False, indent=2)
                    logger.info(
                        f"Saved intermediate results - {len(results)}/{len(data_items)} items processed"
                    )

            # Save results after each batch completion
            accuracy = (
                correct_count / (success_count + error_count)
                if (success_count + error_count) > 0
                else 0
            )
            final_results = {
                "results": results,
                "metadata": {
                    "total_examples": len(data_items),
                    "processed_examples": len(results),
                    "correct_predictions": correct_count,
                    "accuracy": accuracy,
                    "model": model_path,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            }

            with open(f"{output_file}.temp", "w", encoding="utf-8") as f:
                json.dump(final_results, f, ensure_ascii=False, indent=2)

        except Exception as e:
            error_count += 1
            logger.error(f"Error processing batch: {e}")
            # Save what we have so far
            with open(f"{output_file}.temp", "w", encoding="utf-8") as f:
                json.dump({"results": results}, f, ensure_ascii=False, indent=2)

    # Calculate final accuracy
    accuracy = correct_count / len(data_items) if len(data_items) > 0 else 0

    # Save final results to JSON file
    try:
        # Sort results by ID before saving
        results.sort(key=lambda x: x.get("id", ""))

        # Add evaluation metrics
        final_results = {
            "results": results,
            "metadata": {
                "total_examples": len(data_items),
                "processed_examples": len(results),
                "correct_predictions": correct_count,
                "accuracy": accuracy,
                "model": model_path,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        }

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(final_results, f, ensure_ascii=False, indent=2)
        logger.info(f"Results saved to {output_file}")
    except Exception as e:
        logger.error(f"Failed to save results file: {e}")

    # Log summary information
    total_time = time.time() - start_time
    logger.info("=" * 60)
    logger.info(f"Processing completed! Total time: {total_time:.2f} seconds")
    logger.info(f"Successfully processed: {success_count}/{len(data_items)}")
    logger.info(f"Processing failures: {error_count}/{len(data_items)}")
    logger.info(f"Correct predictions: {correct_count}/{len(data_items)}")
    logger.info(f"Accuracy: {accuracy*100:.2f}%")
    average_time_per_item = (
        total_time / len(remaining_items) if len(remaining_items) > 0 else 0
    )
    logger.info(
        f"Average processing time per item: {average_time_per_item:.2f} seconds"
    )
    logger.info("=" * 60)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Process FEVEROUS dataset with VLLM batch inference"
    )

    parser.add_argument("--output_file", type=str, help="Path to save results")
    parser.add_argument("--model_path", type=str, help="Model path or identifier")
    parser.add_argument("--log_file", type=str, help="Path to log file")
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Temperature for model generation",
    )
    parser.add_argument(
        "--max_tokens", type=int, default=4096, help="Maximum tokens for model output"
    )
    parser.add_argument(
        "--tensor_parallel_size", type=int, default=2, help="Tensor parallelism size"
    )
    parser.add_argument(
        "--batch_size", type=int, default=16, help="Batch size for inference"
    )
    parser.add_argument(
        "--start_from", type=int, default=0, help="Start processing from this index"
    )
    parser.add_argument("--base_path", type=str, help="Base path for the project")

    return parser.parse_args()


def main():
    args = parse_arguments()

    base_path = None
    if args.base_path and os.path.exists(args.base_path):
        base_path = args.base_path
    else:
        # Try to find base path automatically
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if os.path.basename(current_dir) == "tests":
            base_path = os.path.dirname(current_dir)

    if not base_path:
        print("Error: Unable to find project root directory")
        exit(1)

    print(f"Using root path: {base_path}")

    input_file = os.path.join(base_path, "data/feverous/dev.jsonl")

    output_file = args.output_file
    model_path = args.model_path
    log_file = args.log_file

    max_tokens = args.max_tokens

    start_from = args.start_from

    temperature = args.temperature

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    process_feverous_data_batch(
        input_file=input_file,
        output_file=output_file,
        model_path=model_path,
        log_file=log_file,
        max_tokens=max_tokens,
        temperature=temperature,
        tensor_parallel_size=args.tensor_parallel_size,
        batch_size=args.batch_size,
        start_from=start_from,
    )


if __name__ == "__main__":
    main()
