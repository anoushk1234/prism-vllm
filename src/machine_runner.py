import asyncio
import json
import os
import pickle
import socket
import threading
import time
from typing import Any, Dict, List

import httpx
import numpy as np
from colorama import Fore, Style
from colorama import init as colorama_init
from lmcache.v1.cache_engine import LMCacheEngineBuilder
from lmcache.integration.vllm.utils import ENGINE_NAME
from transformers import AutoTokenizer
from vllm import SamplingParams

import utils.req_batcher as req_batcher

# from lmcache.v1.cache_engine import LMCacheEngineBuilder
from config.settings import HUGGINGFACE_TOKEN, SERVER_HOST, SERVER_PORT
from utils.db_utils import get_active_peers, register_peer
from utils.deployment_utils import (
    deploy_model_orchestrator,
    report_deployment_completion,
)
from utils.gpu_utils import format_metrics_for_db, get_system_metrics
from utils.inference_utils import (
    CONTEXT_LOCK,
    INFERENCE_CONTEXT,
    STEP_EVENTS,
    STEP_EVENTS_SAMPLER,
    register_inference_hooks,
)
from utils.message_processing import (
    extract_request_metadata,
    log_message_received,
    parse_deployment_message,
    parse_dispatch_message,
    parse_inference_trigger_message,
    parse_json_from_tensor,
    parse_request_message,
)

## Tensor_Iroh Starts here ###########################
from utils.tensor_protocol_adapter import TensorTransport

######################################################
# FORCE vLLM v0 mode (required for selective layer loading)
os.environ["VLLM_USE_V1"] = "0"
print(
    "🔧 FORCED vLLM v0 mode (VLLM_USE_V1=0) for selective layer loading compatibility"
)


# Global TensorTransport instance (lazy-started in main) #######################
tensor_transport: TensorTransport | None = None
deployed_model = None  # this is the global variable for the deployed model
# ?
peer_ticket_map: dict[
    str, str
] = {}  # peer_id  → ticket string (filled in heartbeat) #? Check if hostnames are being set even
current_peer_ticket = None  # this is the global variable for the current peer ticket
deployment_status = "idle"  # idle, downloading, loading, ready, failed
start_inference_run = None  # this is the global variable for the inference run
assigned_layers_global = []  # this is the global variable for the assigned layers
central_server_ticket = (
    None  # this is the global variable for the central server ticket
)
################################################################################

colorama_init(autoreset=True)
COLORS = [Fore.CYAN, Fore.MAGENTA, Fore.YELLOW, Fore.GREEN, Fore.BLUE]
PEER_COLOR = COLORS[
    int(socket.gethostname().__hash__()) % len(COLORS)
]  # deterministic per host
PEER_COLOR = COLORS[
    int(socket.gethostname().__hash__()) % len(COLORS)
]  # deterministic per host

# Batcher for this peer only if first peer
batcher = None
MAX_TIME_PER_BATCH = 1  # Max time we want to wait to fill up batches (in seconds)
pipeline = None


request_metadata = {}  # Maps request-id to dictionary of metadata
batch_metadata = {}  # Maps batch_id to metadata

# ============================================================================
# UNIFIED MESSAGE GATEWAY - SINGLE POINT FOR ALL TENSOR TRANSPORT MESSAGES
# ============================================================================


async def unified_message_gateway():
    """
    Single gateway that receives ALL messages from tensor_transport and routes them
    to the appropriate handlers. This prevents multiple monitors from competing
    for the same message queue.
    """
    global tensor_transport, start_inference_run, current_peer_ticket
    print("🌐 Starting UNIFIED Message Gateway...")

    while True:
        try:
            # Single point of message reception
            task = asyncio.create_task(tensor_transport.recv())
            msg = await task
            if msg is None:
                await asyncio.sleep(0)
                continue

            # Get message metadata
            name = msg.get("name", "")
            tensor = msg.get("tensor")

            if tensor is None:
                print("⚠️ Received message without tensor payload")
                continue

            # print(
            #     f"📨 Gateway received message: '{name}' (tensor shape: {tensor.shape if hasattr(tensor, 'shape') else 'unknown'})"
            # )

            # Route message based on name/content
            try:
                # 1. DEPLOYMENT INSTRUCTIONS (highest priority)
                if (
                    name.lower() in ["deploy", "deployment"] or "deploy" in name.lower()
                ):  # ?
                    await handle_deployment_message(tensor)

                # 2. INFERENCE TRIGGERS
                elif (
                    name.lower() in ["inference", "inference_trigger"]
                    or "inference" in name.lower()
                ):
                    await handle_inference_trigger_message(tensor)

                # 3. INFERENCE DATA (hidden states, residuals, sampler outputs)
                elif any(
                    keyword in name
                    for keyword in [
                        "_hidden_state",
                        "_residual",
                        "_sampler_output",
                        "_combined",
                    ]
                ):
                    await handle_inference_data_message(name, tensor)

                # 5. Receive request
                elif name.lower() == "request":
                    await handle_request_message(tensor)

                # 6. Receive batch information
                elif name.lower() == "dispatch":
                    handle_dispatch_message(tensor)

                # 4. UNKNOWN MESSAGE TYPE
                else:
                    print(
                        f"⚠️ Unknown message type: '{name}' - attempting to parse as JSON"
                    )
                    await handle_unknown_message(name, tensor)

            except Exception as e:
                print(f"❌ Error routing message '{name}': {e}")
                import traceback

                traceback.print_exc()

        except asyncio.CancelledError:
            print("unified_message_gateway cancelled, exiting cleanly.")
            return
        except Exception as e:
            print(f"❌ Error in unified message gateway: {e}")
            await asyncio.sleep(0.0001)


async def handle_deployment_message(tensor):
    """Handle deployment instruction messages"""

    try:
        # Parse deployment message using utility function
        instruction = parse_deployment_message(tensor)
        if not instruction:
            print("❌ Invalid deployment message format")
            return

        instructions = instruction.get("instructions", {})

        # Log deployment message received
        extra_info = f"Model: {instructions.get('model_name', 'unknown')}, Layers: {instructions.get('assigned_layers', [])}"
        log_message_received("deployment", instruction, extra_info)

        # Deploy model in background
        asyncio.create_task(deploy_model_from_instructions(instructions))

    except Exception as e:
        print(f"❌ Error handling deployment message: {e}")
        import traceback

        traceback.print_exc()


async def handle_inference_trigger_message(tensor):
    """Handle inference trigger messages"""
    global start_inference_run, current_peer_ticket

    try:
        # Parse inference trigger message using utility function
        trigger = parse_inference_trigger_message(tensor)
        if not trigger:
            print("❌ Invalid inference trigger message format")
            return

        batch_id = trigger.get("batch_id")
        pipeline = trigger.get("pipeline")

        # Log inference trigger received
        extra_info = (
            f"Batch ID: {batch_id}, Input: {trigger.get('input_text', '')[:50]}..."
        )
        log_message_received("inference_trigger", trigger, extra_info)

        # Initialize INFERENCE_CONTEXT for this request
        with CONTEXT_LOCK:
            if batch_id not in INFERENCE_CONTEXT:
                INFERENCE_CONTEXT[batch_id] = {}

        # Check if inference runner is initialized
        if not start_inference_run:
            print("❌ start_inference_run not initialized! Cannot start inference.")
            return

        # ALL PEERS should start inference!
        peer_position = (
            pipeline.index(current_peer_ticket)
            if current_peer_ticket in pipeline
            else -1
        )

        if peer_position == -1:
            print(f"❌ This peer ({current_peer_ticket}) is not in the pipeline!")
            return

        is_first_peer = peer_position == 0

        print(
            f"📍 This peer is {'FIRST' if is_first_peer else f'#{peer_position + 1}'} in the pipeline"
        )
        print(
            f"{'✅ Starting inference as FIRST peer' if is_first_peer else '⏳ Starting inference and waiting for tensors from previous peer'}"
        )

        # Start inference in background thread for ALL peers
        try:
            from vllm import SamplingParams

            loop = asyncio.get_running_loop()

            # Extract parameters
            input_text_list = trigger.get("input_text", "")
            sampling_params = [
                SamplingParams(**item) for item in trigger.get("sampling_params")
            ]
            _ = trigger.get("assigned_layers", {})

            print("�� Starting inference run in background thread...")

            # print(
            #     f"THREAD - {threading.current_thread().name}, {threading.current_thread().ident}"
            # )
            # print(f"LOOP - {id(loop)}, {loop}")
            _ = loop.run_in_executor(
                None,
                start_inference_run,
                batch_id,
                pipeline,
                input_text_list,
                sampling_params,
            )

            print(f"✅ Inference started for request {batch_id}")

        except Exception as e:
            print(f"❌ Failed to start inference: {e}")
            import traceback

            traceback.print_exc()

    except Exception as e:
        print(f"❌ Error handling inference trigger: {e}")
        import traceback

        traceback.print_exc()


async def handle_inference_data_message(name: str, tensor):
    """Handle inference data messages (combined tensors or sampler outputs)"""
    try:
        # print(f"📊 Processing inference data: {name}")

        # Parse the tensor name using utility function
        metadata = extract_request_metadata(name)
        if not metadata:
            print(f"❌ Invalid tensor name format: {name}")
            return

        request_id, step_idx, message_type = metadata
        # print(
        #     f"📋 Parsed: request_id='{request_id}', step={step_idx}, type='{message_type}'"
        # )

        if message_type == "combined":
            # if hasattr(tensor, "numpy"):
            #     # PyTorch tensor - convert to numpy
            #     arr = tensor.numpy()
            # else:
            #     # Already numpy
            #     arr = tensor

            # Unpickle the combined tensor
            combined_tensor = pickle.loads(tensor.numpy().tobytes())

            hidden_state = combined_tensor[0]
            residual = combined_tensor[1]
            positions = combined_tensor[2]

            # print(
            #     f"✅ Stored both hidden_state and residual and positions for {request_id} step {step_idx}"
            # )

            # # Unstack the combined tensor
            # if tensor.shape[0] != 2:
            #     print(f"❌ Invalid combined tensor shape: {tensor.shape}")
            #     return

            # hidden_state = tensor[0]
            # residual = tensor[1]

            # Store in INFERENCE_CONTEXT
            with CONTEXT_LOCK:
                if request_id not in INFERENCE_CONTEXT:
                    INFERENCE_CONTEXT[request_id] = {}
                if str(step_idx) not in INFERENCE_CONTEXT[request_id]:
                    INFERENCE_CONTEXT[request_id][str(step_idx)] = {}

                INFERENCE_CONTEXT[request_id][str(step_idx)]["hidden_state"] = (
                    hidden_state
                )
                INFERENCE_CONTEXT[request_id][str(step_idx)]["residual"] = residual
                INFERENCE_CONTEXT[request_id][str(step_idx)]["positions"] = positions
                # print(
                #     f"✅ Stored both hidden_state and residual and positions for {request_id} step {step_idx}"
                # )

                # wake anybody waiting for this step's payload
                event = STEP_EVENTS[request_id].setdefault(step_idx, threading.Event())
                event.set()

        elif message_type == "sampler_output":
            # Convert tensor to numpy array first, then unpickle
            if hasattr(tensor, "numpy"):
                # PyTorch tensor - convert to numpy
                arr = tensor.numpy()
            else:
                # Already numpy
                arr = tensor

            # Unpickle the sampler output
            sampler_output = pickle.loads(arr.tobytes())

            with CONTEXT_LOCK:
                if request_id not in INFERENCE_CONTEXT:
                    INFERENCE_CONTEXT[request_id] = {}
                if str(step_idx) not in INFERENCE_CONTEXT[request_id]:
                    INFERENCE_CONTEXT[request_id][str(step_idx)] = {}

                INFERENCE_CONTEXT[request_id][str(step_idx)]["sampler_output"] = (
                    sampler_output
                )
                # print(f"✅ Stored sampler_output for {request_id} step {step_idx}")

                # wake anybody waiting for this step's sampler output
                event = STEP_EVENTS_SAMPLER[request_id].setdefault(
                    step_idx, threading.Event()
                )
                event.set()

    except Exception as e:
        print(f"❌ Error handling inference data '{name}': {e}")
        import traceback

        traceback.print_exc()


async def handle_request_message(tensor):
    global batcher

    try:
        req = parse_request_message(tensor)

        request_metadata[req["request_id"]] = {
            "prompt": req["prompt"],
            "sampling_params": req["sampling_params"],
        }
        print(f"handle_request_message - req: {req}")

        index = pipeline.index(current_peer_ticket)

        # If first peer, queue and batch incoming requests
        # If not first, store requests, but not in charge of batching
        if index == 0:
            # Initialize batcher
            if batcher is None:
                print("handle_request_message() index == 0, batcher is None")
                print(
                    f"THREAD - {threading.current_thread().name}, {threading.current_thread().ident}"
                )
                loop = asyncio.get_running_loop()
                print(f"asyncio loop - {id(loop)}, {loop}")
                batcher = req_batcher.Batcher(
                    model_name=req["model"],
                    max_req=req["max_batch_size"],
                    max_time=MAX_TIME_PER_BATCH,
                    process_req_fn=dispatch_batch,
                )

            # Add request to batcher
            await asyncio.create_task(
                batcher.add(
                    req_batcher.Request(
                        id=req["request_id"],
                        prompt=req["prompt"],
                        model_name=req["model"],
                        sampling_params=req["sampling_params"],
                    )
                )
            )

    except Exception:
        raise


def handle_dispatch_message(tensor):
    global request_metadata, batch_metadata, batcher

    try:
        msg = parse_dispatch_message(tensor)
        batch_id = msg["batch_id"]
        request_ids = msg["request_id"]
        print(f"handle_dispatch_message - batch: {batch_id}, reqs: {request_ids}")

        batch_metadata[batch_id] = {"request_id": request_ids}

        loop = asyncio.get_running_loop()
        _ = loop.run_in_executor(
            None,
            start_inference_run,
            batch_id,
            pipeline,
            [request_metadata[req]["prompt"] for req in request_ids],
            [
                SamplingParams(**request_metadata[req]["sampling_params"])
                for req in request_ids
            ],
            batcher,
        )

    except Exception:
        raise


async def handle_unknown_message(name: str, tensor):
    """Handle unknown message types by attempting to parse as JSON"""
    try:
        # Parse message using utility function
        parsed = parse_json_from_tensor(tensor)
        if not parsed:
            print(f"❌ Could not parse unknown message '{name}'")
            return

        print(f"📋 Unknown message '{name}' parsed as JSON:")
        print(f"   Keys: {list(parsed.keys())}")

    except Exception as e:
        print(f"❌ Error handling unknown message '{name}': {e}")


# Create batch in a separate thread
# Inform downstream peers of new batch
async def dispatch_batch(
    batch_id: str, model_name: str, queue: List[req_batcher.Request]
):
    global start_inference_run, pipeline, batch_metadata, tensor_transport, batcher

    try:
        request_ids = [req.id for req in queue]
        batch_metadata[batch_id] = {"request_id": request_ids}

        payload = {"batch_id": batch_id, "request_id": request_ids}
        payload = json.dumps(payload).encode()
        payload = np.frombuffer(payload, dtype=np.uint8)

        # Send batch info to all peers in pipeline
        tasks = []
        for peer_ticket in pipeline[1:]:
            tasks.append(
                asyncio.create_task(
                    tensor_transport.send(peer_ticket, "dispatch", payload)
                )
            )

        # Send batch info to server
        server_url = f"http://{SERVER_HOST}:{SERVER_PORT}"
        print("HERE ",server_url)
        server_task = asyncio.create_task(
            send_batch_info(batch_id, request_ids, server_url)
        )
        tasks.append(server_task)

        loop = asyncio.get_running_loop()
        _ = loop.run_in_executor(
            None,
            start_inference_run,
            batch_id,
            pipeline,
            [req.prompt for req in queue],
            [SamplingParams(**req.sampling_params) for req in queue],
            batcher,
        )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        print(results)

    except:
        raise


async def send_batch_info(
    batch_id: str, request_id: List[str], server_url: str = "http://{SERVER_IP}:8000"
):
    try:
        payload = {"batch_id": batch_id, "request_id": request_id}

        async with httpx.AsyncClient() as client:
            response = await client.post(f"{server_url}/batch_info", json=payload)
            response.raise_for_status()

    except Exception as e:
        print(f"[ERROR] send_batch_info - {e}")


# ============================================================================
# FILE DOWNLOAD AND DEPLOYMENT LOGIC - MOVED TO deployment_utils.py
# ============================================================================


async def deploy_model_from_instructions(instructions: Dict[str, Any]) -> bool:
    """
    Deploy model based on deployment instructions received from server.

    This is now a simplified orchestrator that uses the specialized functions
    from deployment_utils.py for actual implementation.
    """
    global \
        deployed_model, \
        deployment_status, \
        start_inference_run, \
        tensor_transport, \
        pipeline

    model_name = instructions.get("model_name", "unknown")

    # Check if model is already successfully deployed
    if deployment_status == "ready" and deployed_model is not None:
        print(
            f"✅ Model {model_name} already successfully deployed, skipping new deployment"
        )
        return True

    # Prevent multiple concurrent deployments
    if deployment_status in ["downloading", "loading"]:
        print(f"⚠️ Deployment already in progress ({deployment_status}), skipping...")
        return False

    try:
        # Update status to indicate deployment started
        deployment_status = "downloading"

        # Use the new orchestrator to handle the deployment
        success, loaded_model = await deploy_model_orchestrator(instructions)

        # vllm.LLM (non-async)
        if hasattr(loaded_model, "llm_engine"):
            print(type(loaded_model), dir(loaded_model))
            executor = loaded_model.llm_engine.model_executor
            max_concurrency = (
                executor.cache_config.num_gpu_blocks * executor.cache_config.block_size
            ) / executor.model_config.max_model_len
            print(f"Max concurrency is {max_concurrency}")

        if not success or loaded_model is None:
            print("❌ Model deployment orchestration failed")
            deployment_status = "failed"
            await report_deployment_completion(
                server_host=SERVER_HOST,
                server_port=SERVER_PORT,
                model_name=model_name,
                peer_id=current_peer_ticket,
                success=False,
                max_req_in_batch=5,
            )
            return False

        # Update global state with successfully loaded model
        deployed_model = loaded_model
        deployment_status = "loading"  # Update status before inference setup

        # Get tokenizer https://huggingface.co/docs/transformers/fast_tokenizers
        pipeline = instructions.get("pipeline")
        if current_peer_ticket == pipeline[-1]:
            if "mistral" in model_name.lower() or "devstral" in model_name.lower():
                from mistral_common.tokens.tokenizers.mistral import MistralTokenizer

                local_model_dir = f"deployed_models/{model_name}"
                tokenizer = MistralTokenizer.from_file(
                    f"{local_model_dir}/config/tekken.json"
                )
            else:
                tokenizer = AutoTokenizer.from_pretrained(
                    model_name, token=HUGGINGFACE_TOKEN
                )
        else:
            tokenizer = None

        # Setup inference hooks
        print("✅ Model loaded successfully, registering inference hooks...")
        server_url = f"http://{SERVER_HOST}:{SERVER_PORT}"
        start_inference_run = register_inference_hooks(
            llm=deployed_model,
            node=tensor_transport,
            peer_id=current_peer_ticket,
            server_url=server_url,
            next_peer_ticket=instructions.get("next_peer_ticket"),
            pipeline=instructions.get("pipeline"),
            tokenizer=tokenizer,
        )

        # print(type(deployed_model))
        # print(deployed_model)

        # Update global assigned layers
        global assigned_layers_global
        assigned_layers_global = instructions["assigned_layers"]

        # Mark deployment as ready
        deployment_status = "ready"
        print("🔍 [DEBUG] Deployment status set to ready")

        # Report success to server
        await report_deployment_completion(
            server_host=SERVER_HOST,
            server_port=SERVER_PORT,
            model_name=model_name,
            peer_id=current_peer_ticket,
            success=True,
            max_req_in_batch=int(max_concurrency),
        )

        return True

    except Exception as e:
        print(f"❌ Model deployment failed: {e}")
        deployment_status = "failed"

        # Report failure to server
        try:
            await report_deployment_completion(
                server_host=SERVER_HOST,
                server_port=SERVER_PORT,
                model_name=model_name,
                peer_id=current_peer_ticket,
                success=False,
                max_req_in_batch=5,
            )
        except Exception as _:
            pass  # Don't fail on reporting failure

        return False


# report_deployment_completion moved to deployment_utils.py


def get_model_status() -> Dict[str, Any]:
    """Get current model deployment status."""
    global deployed_model, deployment_status

    return {
        "status": deployment_status,
        "model_loaded": deployed_model is not None,
        "ready_for_inference": deployment_status == "ready"
        and deployed_model is not None,
    }


# ============================================================================
# EXISTING FUNCTIONALITY (Matrix computation, heartbeat, etc.)
# ============================================================================


async def http_heartbeat_loop(current_peer_ticket: str, interval_s: float = 1.0):
    """Send heartbeat to central server over HTTP and exit if server stops responding."""
    global central_server_ticket
    consecutive_failures = 0
    max_failures = 10  # 10 seconds tolerance
    server_url = f"http://{SERVER_HOST}:{SERVER_PORT}/heartbeat"
    print(f"HB {server_url}")
    server_added = False

    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            try:
                # Offload potentially blocking metrics collection
                metrics = await asyncio.to_thread(get_system_metrics)
                metrics_dict = format_metrics_for_db(metrics)
                _ = metrics_dict["total_free_vram_gb"]

                payload = {
                    "peer_id": current_peer_ticket,
                    **{k: v for k, v in metrics_dict.items() if k != "timestamp"},
                    "gpu_count": len(metrics.gpu_info),
                    "timestamp": int(time.time()),
                }
                print(payload)
                response = await client.post(server_url, json=payload)
                print(f"response: {response} | {server_url}")
                if response.status_code == 200:
                    data = response.json()
                    # Only add server to network once
                    if not server_added:
                        central_server_ticket = data["central_server_ticket"]
                        print(f"🔗 Central server ticket: {central_server_ticket}")
                        server_added = True
                    # print(f"{PEER_COLOR}💓 Sent heartbeat | CPU {metrics.cpu_percent:.1f}% VRAM {total_free_vram:.1f} GB → ACK {Style.RESET_ALL}")
                    # print(f"{PEER_COLOR}💓 Sent heartbeat | CPU {metrics.cpu_percent:.1f}% VRAM {total_free_vram:.1f} GB → ACK {Style.RESET_ALL}")
                else:
                    consecutive_failures += 1
                    print("HB failed")
                    print(
                        f"{PEER_COLOR}⚠️ Heartbeat HTTP {response.status_code}{Style.RESET_ALL}"
                    )
            except Exception as e:
                print(f"HB exception {e}")
                consecutive_failures += 1
                print(f"{PEER_COLOR}⚠️ Heartbeat error: {e}{Style.RESET_ALL}")
            if consecutive_failures >= max_failures:
                print(
                    f"{PEER_COLOR}💔 Lost contact with server, shutting down peer{Style.RESET_ALL}"
                )
                os._exit(1)
            await asyncio.sleep(interval_s)


async def main():
    """Main function to run the distributed computation node"""
    global current_peer_ticket, peer_ticket_map, tensor_transport

    loop = asyncio.get_running_loop()
    print(f"In main - asyncio loop is {id(loop)}")

    # Set up Tensor_Iroh and get the ticket #################################
    tensor_transport = TensorTransport()
    await tensor_transport.start()
    current_peer_ticket = tensor_transport.ticket
    print(f"🪪 TensorTransport started – ticket:\n{current_peer_ticket}\n")

    # Set up a unique data directory for this node
    hostname = socket.gethostname()
    ticket_and_hostname = f"Ticket: {current_peer_ticket}, Hostname: {hostname}"
    peer_ticket_map[ticket_and_hostname] = (
        current_peer_ticket  # check if we even need ticket_and_hostname
    )

    print(
        f"🤖 Running as peer: {current_peer_ticket}"
    )  # ? Misleading, does our use of the term peer mean that it is registered w server?
    heartbeat_task = asyncio.create_task(http_heartbeat_loop(current_peer_ticket))
    LONG = os.environ.get("LONG",42.5907715)
    LAT = os.environ.get("LAt",42.5907715)
    await register_peer(current_peer_ticket, hostname, 10, 15)
    print(f"✅ Registered in MongoDB as {current_peer_ticket}")

    # Main gateway to receive web requests
    print("Starting unified message gateway...")
    gateway_task = asyncio.create_task(unified_message_gateway())
    # debug_monitor_task = asyncio.create_task(debug_inference_context_monitor())
    await asyncio.sleep(1)  # ?

    try:  # ? Why is this specific portion in a try block
        # Wait until this peer is included in the pipeline configuration
        print("⏳ Waiting to be included in the pipeline...")
        while True:  # ? This block seems strange. get_active_peers checks the same thing as register_peer, which we just await-ed
            pipeline = await get_active_peers()  # ? the ordering of peers in the pipeline is order of addition to db, is this correct?
            print(f"🔗 Pipeline: {pipeline}")
            if current_peer_ticket in pipeline:
                print(f"✅ Included in pipeline as {current_peer_ticket}")
                break
            await asyncio.sleep(2)

        # Determine this peer's position and role in the pipeline
        index = pipeline.index(current_peer_ticket)
        is_first = index == 0
        is_last = index == len(pipeline) - 1
        _ = pipeline[index + 1] if not is_last else None

        print(f"✅ Position: {index} | First: {is_first} | Last: {is_last}")

        try:
            await gateway_task
        except Exception as e:
            print(f"Gateway crash: {e}")

    except Exception as e:
        print(f"❌ Error in main loop: {e}")
    finally:
        # Cancel background tasks
        heartbeat_task.cancel()
        gateway_task.cancel()
        LMCacheEngineBuilder.destroy(ENGINE_NAME)
        # debug_monitor_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        try:
            await gateway_task
        except asyncio.CancelledError:
            pass
        # try:
        #     await debug_monitor_task
        # except asyncio.CancelledError:
        #     pass
        # Deregister peer when shutting down
        # await deregister_peer(current_peer_ticket)
        print(f"👋 Deregistered {current_peer_ticket} from pipeline")

    #########################################################################


if __name__ == "__main__":
    asyncio.run(main())
