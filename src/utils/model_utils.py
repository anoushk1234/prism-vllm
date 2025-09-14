import json
import math
from typing import Any, Dict, List, Tuple

import aiohttp
from huggingface_hub import get_safetensors_metadata

from src.config.settings import DEFAULT_CONFIG_FILENAME, HUGGINGFACE_TOKEN


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great circle distance between two points on Earth in kilometers.
    
    Args:
        lat1, lon1: Latitude and longitude of first point in decimal degrees
        lat2, lon2: Latitude and longitude of second point in decimal degrees
        
    Returns:
        Distance in kilometers
    """
    # Convert decimal degrees to radians
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    
    # Haversine formula
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    
    # Radius of earth in kilometers
    r = 6371
    return c * r

def calculate_node_score(carbon_intensity: float, 
                         vram_gb: float, 
                         distance_km: float, 
                         weight_carbon: float = 0.6, 
                         weight_vram: float = 0.2, 
                         weight_distance: float = 0.2,
                        # Normalization anchors (tune/learn per fleet)
                        carbon_max: float = 1000.0,   # gCO2/kWh
                        vram_max: float = 100.0,      # GB
                        dist_max_km: float = 10_000.0 # cap at intercontinental
                        ) -> float:
    """
    Calculate a composite score for peer selection.
    Lower scores are better (prefer lower carbon intensity and higher VRAM).
    
    Args:
        carbon_intensity: Carbon intensity in gCO2/kWh (lower is better)
        vram_gb: Available VRAM in GB (higher is better)
        weight_carbon: Weight for carbon intensity (default 0.7)
        weight_vram: Weight for VRAM (default 0.3)
        
    Returns: 
        Composite score (lower is better)
    """
    # Normalize carbon intensity (assume range 0-1000 gCO2/kWh, lower is better)
    carbon_score = carbon_intensity / carbon_max
    
    # Normalize VRAM (assume range 0-100 GB, higher is better, so invert)
    vram_score = 1.0 - min(vram_gb / vram_max, 1.0)

    # Normalize distance (assume range 0-1000 km, lower is better)
    dist_score = math.log1p(min(distance_km, dist_max_km)) / math.log1p(dist_max_km)

    return weight_carbon * carbon_score + weight_vram * vram_score + weight_distance * dist_score


def calculate_edge_score(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate edge score for travel distance optimization.
    Lower scores are better (prefer shorter distances).
    
    Args:
        lat1, lon1: Coordinates of first peer
        lat2, lon2: Coordinates of second peer
        
    Returns:
        Edge score (lower is better)
    """
    distance_km = haversine_distance(lat1, lon1, lat2, lon2)
    # Convert distance to score (lower distance = lower score)
    return distance_km


def nearest_neighbor_tsp(coords: List[Tuple[str, float, float]], start_peer: str) -> List[str]:
    """
    Solve TSP using nearest neighbor heuristic starting from a specific peer.
    
    Args:
        coords: List of (peer_id, lat, lon) tuples
        start_peer: Peer ID to start the tour from
        
    Returns:
        Ordered list of peer IDs representing the tour
    """
    if not coords:
        return []
    
    # Find starting peer coordinates
    start_coords = None
    remaining = []
    for peer_id, lat, lon in coords:
        if peer_id == start_peer:
            start_coords = (lat, lon)
        else:
            remaining.append((peer_id, lat, lon))
    
    if start_coords is None:
        # If start peer not found, use first peer
        start_coords = (coords[0][1], coords[0][2])
        remaining = coords[1:]
    
    tour = [start_peer]
    current_lat, current_lon = start_coords
    
    while remaining:
        # Find nearest neighbor
        min_distance = float('inf')
        nearest_idx = 0
        
        for i, (peer_id, lat, lon) in enumerate(remaining):
            distance = haversine_distance(current_lat, current_lon, lat, lon)
            if distance < min_distance:
                min_distance = distance
                nearest_idx = i
        
        # Add nearest neighbor to tour
        nearest_peer, nearest_lat, nearest_lon = remaining.pop(nearest_idx)
        tour.append(nearest_peer)
        current_lat, current_lon = nearest_lat, nearest_lon
    
    return tour


def two_opt_improvement(tour: List[str], coords_dict: Dict[str, Tuple[float, float]]) -> List[str]:
    """
    Apply 2-opt improvement to reduce total tour distance.
    
    Args:
        tour: Current tour as list of peer IDs
        coords_dict: Dictionary mapping peer_id to (lat, lon)
        
    Returns:
        Improved tour
    """
    if len(tour) <= 3:
        return tour
    
    improved = True
    best_tour = tour.copy()
    
    while improved:
        improved = False
        best_distance = calculate_tour_distance(best_tour, coords_dict)
        
        for i in range(1, len(tour) - 1):
            for j in range(i + 1, len(tour)):
                # Try 2-opt swap
                new_tour = tour[:i] + tour[i:j+1][::-1] + tour[j+1:]
                new_distance = calculate_tour_distance(new_tour, coords_dict)
                
                if new_distance < best_distance:
                    best_tour = new_tour
                    best_distance = new_distance
                    improved = True
                    break
            
            if improved:
                break
        
        tour = best_tour
    
    return best_tour


def calculate_tour_distance(tour: List[str], coords_dict: Dict[str, Tuple[float, float]]) -> float:
    """
    Calculate total distance of a tour.
    
    Args:
        tour: List of peer IDs in order
        coords_dict: Dictionary mapping peer_id to (lat, lon)
        
    Returns:
        Total distance in kilometers
    """
    if len(tour) <= 1:
        return 0.0
    
    total_distance = 0.0
    for i in range(len(tour) - 1):
        peer1, peer2 = tour[i], tour[i + 1]
        lat1, lon1 = coords_dict[peer1]
        lat2, lon2 = coords_dict[peer2]
        total_distance += haversine_distance(lat1, lon1, lat2, lon2)
    
    return total_distance


async def download_config(
    model_id: str, hf_token: str = None, filename: str = DEFAULT_CONFIG_FILENAME
) -> Dict[str, Any]:
    """
    Asynchronously download model configuration from HuggingFace.

    Args:
        model_id: HuggingFace model ID
        hf_token: HuggingFace API token (optional, uses default if not provided)
        filename: Configuration filename

    Returns:
        Model configuration dictionary
    """
    url = f"https://huggingface.co/{model_id}/raw/main/{filename}"
    headers = {
        "Authorization": f"Bearer {hf_token or HUGGINGFACE_TOKEN}",
        "Accept": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                
                return {
    "architectures": [
        "LlamaForCausalLM"
    ],
    "attention_bias": False,
    "attention_dropout": 0.0,
    "bos_token_id": 128000,
    "eos_token_id": 128001,
    "head_dim": 64,
    "hidden_act": "silu",
    "hidden_size": 2048,
    "initializer_range": 0.02,
    "intermediate_size": 8192,
    "max_position_embeddings": 131072,
    "mlp_bias": False,
    "model_type": "llama",
    "num_attention_heads": 32,
    "num_hidden_layers": 16,
    "num_key_value_heads": 8,
    "pretraining_tp": 1,
    "rms_norm_eps": 1e-05,
    "rope_scaling": {
        "factor": 32.0,
        "high_freq_factor": 4.0,
        "low_freq_factor": 1.0,
        "original_max_position_embeddings": 8192,
        "rope_type": "llama3"
    },
    "rope_theta": 500000.0,
    "tie_word_embeddings": True,
    "torch_dtype": "bfloat16",
    "transformers_version": "4.45.0.dev0",
    "use_cache": True,
    "vocab_size": 128256
}
                
                if response.status == 404:
                    raise Exception(f"Model or file not found: {model_id}/{filename}")
                elif response.status == 401:
                    raise Exception(
                        "Unauthorized: Please check your Hugging Face token"
                    )
                elif response.status != 200:
                    raise Exception(
                        f"Error downloading {filename} from model {model_id}. HTTP {response.status}"
                    )

                # Try to parse as JSON first
                try:
                    return await response.json()
                except Exception as _:
                    # If JSON parsing fails, try to parse the text content
                    text_content = await response.text()
                    try:
                        return json.loads(text_content)
                    except Exception as _:
                        raise Exception(
                            f"Failed to parse response as JSON. Response content: {text_content[:200]}..."
                        )
        except aiohttp.ClientError as e:
            raise Exception(f"Network error while accessing Hugging Face: {str(e)}")


# llm-mem's byte mapping
BYTES_PER_DTYPE = {
    "int4": 0.5,
    "int8": 1,
    "float8": 1,
    "float16": 2,
    "float32": 4,
    "bfloat16": 2,
}


def estimate_parameters(model_id: str, hf_token: str) -> int:
    """Get actual parameter count from safetensors metadata (llm-mem approach)"""
    try:
        metadata = get_safetensors_metadata(model_id, token=hf_token)
        if not metadata or not metadata.parameter_count:
            print("Could not fetch metadata")
            return 0

        # Sum all parameter counts (handles multi-file models)
        total_params = sum(int(count) for count in metadata.parameter_count.values())
        return total_params
    except Exception as e:
        print(f"Error getting safetensors metadata: {e}")
        return 0


def estimate_vram(parameters_billions: float, dtype: str) -> float:
    """Apply llm-mem's exact formula for model memory"""
    bytes_val = BYTES_PER_DTYPE[dtype]
    # llm-mem's formula: (params * 4) / (32/(bytes*8)) * 1.18
    # return round((parameters_billions * 4) / (32 / (bytes_val * 8)) * 1.18, 2)
    MAGIC_KNOB_FOR_ACTIVATION_OVERHEAD = 1.21
    return round(
        (parameters_billions * 4) / (32 / (bytes_val * 8))
        + MAGIC_KNOB_FOR_ACTIVATION_OVERHEAD,
        2,
    )


# def estimate_parameters(config: Dict[str, Any]) -> int:
#     """
#     Estimate the total number of parameters of a Transformer model based on its config.

#     Args:
#         config: Model configuration dictionary

#     Returns:
#         Total number of parameters
#     """
#     vocab_size = config.get("vocab_size")
#     hidden_size = config.get("hidden_size")
#     num_hidden_layers = config.get("num_hidden_layers")
#     num_attention_heads = config.get("num_attention_heads")
#     head_dim = config.get("head_dim")
#     if head_dim is None:
#         head_dim = hidden_size // num_attention_heads
#     num_key_value_heads = config.get("num_key_value_heads")
#     intermediate_size = config.get("intermediate_size")

#     if None in [vocab_size, hidden_size, num_hidden_layers, num_attention_heads, num_key_value_heads, intermediate_size]:
#         raise ValueError("The config is missing one or more required parameters.")

#     embed_params = vocab_size * hidden_size
#     q_params = hidden_size * (num_attention_heads * head_dim)
#     k_params = hidden_size * (num_key_value_heads * head_dim)
#     v_params = hidden_size * (num_key_value_heads * head_dim)
#     o_params = (num_attention_heads * head_dim) * hidden_size
#     attn_params = q_params + k_params + v_params + o_params
#     mlp_params = 2 * hidden_size * intermediate_size
#     layer_params = attn_params + mlp_params
#     total_params = embed_params + num_hidden_layers * layer_params
#     return total_params

# def estimate_vram(total_params: int, q_bits: int) -> float:
#     """
#     Estimate VRAM requirements for a model.

#     Args:
#         total_params: Total number of model parameters
#         q_bits: Quantization bits (e.g., 16 for FP16)

#     Returns:
#         Estimated VRAM requirement in GB
#     """
#     P_in_billions = total_params / 1e9
#     return P_in_billions * 4 * (q_bits / 32) * 1.2


# [HETARTH] TODO: This is a hack to account for the activation overhead. THIS WILL FUCK US UP SOMEDAY - SO PROFILE IT!!
def estimate_layer_vram(
    config: Dict[str, Any],
    q_bits: int,
    batch_size: int = 1,
    seq_length: int = 2048,
    include_kv_cache: bool = False,
) -> Tuple[float, float, float]:
    """
    Estimate VRAM requirements per layer and for non-layer components (weights + activations).

    Notes:
        - This function estimates both weights memory and activation memory.
        - Embeddings include the input embedding matrix plus LM head if weights are not tied.
        - Per-layer parameters include attention, 3-projection MLP (gate/up/down), and layer norms.
        - Final output norm is included in the total but not in per-layer or embedding buckets.
        - Activation memory is calculated based on batch size and sequence length.

    Args:
        config: Model configuration dictionary
        q_bits: Quantization bits (e.g., 16 for FP16)
        batch_size: Batch size for activation calculation (default: 1)
        seq_length: Sequence length for activation calculation (default: 2048)
        include_kv_cache: Whether to include KV cache (not implemented yet, default: False)

    Returns:
        Tuple of (vram_per_layer_gb, embedding_vram_gb, total_vram_gb)
    """
    # Get model parameters
    vocab_size = config.get("vocab_size")
    hidden_size = config.get("hidden_size")
    num_hidden_layers = config.get("num_hidden_layers")
    num_attention_heads = config.get("num_attention_heads")
    head_dim = config.get("head_dim")
    if head_dim is None and hidden_size is not None and num_attention_heads:
        head_dim = hidden_size // num_attention_heads
    num_key_value_heads = config.get("num_key_value_heads")
    if num_key_value_heads is None:
        num_key_value_heads = num_attention_heads
    intermediate_size = config.get("intermediate_size")
    tie_word_embeddings = bool(config.get("tie_word_embeddings", False))

    required = [
        vocab_size,
        hidden_size,
        num_hidden_layers,
        num_attention_heads,
        num_key_value_heads,
        intermediate_size,
        head_dim,
    ]
    # print("Required parameters:", required)
    if any(x is None for x in required):
        raise ValueError("The config is missing one or more required parameters.")

    # Embeddings (input) + LM head if not tied
    embed_params = vocab_size * hidden_size
    lm_head_params = 0 if tie_word_embeddings else vocab_size * hidden_size
    embedding_params_total = embed_params + lm_head_params

    # Parameters per transformer layer
    # Attention projections
    q_params = hidden_size * (num_attention_heads * head_dim)
    k_params = hidden_size * (num_key_value_heads * head_dim)
    v_params = hidden_size * (num_key_value_heads * head_dim)
    o_params = (num_attention_heads * head_dim) * hidden_size
    attn_params = q_params + k_params + v_params + o_params

    # MLP: gate, up, down (3 projections for most modern LLMs, e.g., SwiGLU)
    mlp_params = 3 * hidden_size * intermediate_size

    # Layer norms per layer (pre-attn and pre-mlp)
    layer_norm_params = 2 * hidden_size

    layer_params = attn_params + mlp_params + layer_norm_params

    # Final output norm (applied once at the end of the stack)
    final_norm_params = hidden_size

    # Convert to VRAM (GB) for WEIGHTS
    bytes_per_param = q_bits / 8
    bytes_to_gb = 1024**3

    # Calculate WEIGHTS memory
    embedding_vram_gb = (embedding_params_total * bytes_per_param) / bytes_to_gb
    layer_weights_vram_gb = (layer_params * bytes_per_param) / bytes_to_gb

    # Calculate ACTIVATION memory per layer
    # Based on article formula: batch_size * seq_length * hidden_dim * K
    # K is a heuristic factor (using conservative estimate of 12)
    # This represents intermediate activations in attention and MLP blocks
    K_FACTOR = 12  # Conservative heuristic for activation multiplier

    # Activation memory per layer (in bytes)
    activation_bytes_per_layer = (
        batch_size * seq_length * hidden_size * K_FACTOR * (q_bits / 8)
    )
    activation_vram_per_layer_gb = activation_bytes_per_layer / bytes_to_gb

    # Total per-layer VRAM (weights + activations)
    vram_per_layer_gb = layer_weights_vram_gb + activation_vram_per_layer_gb

    # Total model VRAM
    total_vram_gb = (
        embedding_vram_gb
        + (num_hidden_layers * vram_per_layer_gb)
        + (final_norm_params * bytes_per_param) / bytes_to_gb
    )

    return (
        round(vram_per_layer_gb, 6),
        round(embedding_vram_gb, 6),
        round(total_vram_gb, 6),
    )


def calculate_max_layers_for_peer(
    config: Dict[str, Any],
    available_vram_gb: float,
    q_bits: int,
    safety_margin: float = 0.1,
    batch_size: int = 1,
    seq_length: int = 2048,
) -> Dict[str, Any]:
    """
    Calculate the maximum number of layers that can fit in a peer's available VRAM.

    Args:
        config: Model configuration dictionary
        available_vram_gb: Available VRAM in GB for this peer
        q_bits: Quantization bits (e.g., 16 for FP16)
        safety_margin: Safety margin as fraction of available VRAM (default: 10%)
        batch_size: Batch size for activation calculation (default: 1)
        seq_length: Sequence length for activation calculation (default: 2048)

    Returns:
        Dictionary containing layer calculation details
    """
    # Get VRAM requirements with activation overhead
    vram_per_layer_gb, embedding_vram_gb, total_model_vram_gb = estimate_layer_vram(
        config, q_bits, batch_size, seq_length, include_kv_cache=False
    )

    # Apply safety margin
    usable_vram_gb = available_vram_gb * (1 - safety_margin)

    # Calculate max layers (assuming embedding layers are handled separately)
    max_layers = int(usable_vram_gb / vram_per_layer_gb)

    # If this peer needs to handle embeddings too, subtract that from available space
    max_layers_with_embeddings = int(
        (usable_vram_gb - embedding_vram_gb) / vram_per_layer_gb
    )
    max_layers_with_embeddings = max(0, max_layers_with_embeddings)

    # Calculate actual VRAM usage
    vram_used_layers_only = max_layers * vram_per_layer_gb
    vram_used_with_embeddings = embedding_vram_gb + (
        max_layers_with_embeddings * vram_per_layer_gb
    )

    return {
        "max_layers_only": max_layers,
        "max_layers_with_embeddings": max_layers_with_embeddings,
        "vram_per_layer_gb": round(vram_per_layer_gb, 3),
        "embedding_vram_gb": round(embedding_vram_gb, 3),
        "available_vram_gb": available_vram_gb,
        "usable_vram_gb": round(usable_vram_gb, 3),
        "safety_margin": safety_margin,
        "vram_used_layers_only": round(vram_used_layers_only, 3),
        "vram_used_with_embeddings": round(vram_used_with_embeddings, 3),
        "total_model_layers": config.get("num_hidden_layers"),
        "total_model_vram_gb": round(total_model_vram_gb, 3),
        "batch_size": batch_size,
        "seq_length": seq_length,
    }


def distribute_layers_across_peers(
    config: Dict[str, Any],
    peers_vram: Dict[str, float],  # peer_id -> available_vram_gb
    peers_carbon_intensity: Dict[str, float],  # peer_id -> carbon intensity (gCO2/kWh)
    peers_locations: Dict[str, Tuple[float, float]],  # peer_id -> (lat, lon)
    q_bits: int,
    safety_margin: float = 0.1,
    batch_size: int = 1,
    seq_length: int = 2048,
) -> Dict[str, Any]:
    """
    Distribute model layers across multiple GPU peers by VRAM only (capacity balancing).
    Uses balanced distribution to achieve similar VRAM utilization percentage across all peers.

    Args:
        config: Model configuration dictionary
        peers_vram: Dictionary mapping peer_id to available VRAM in GB
        q_bits: Quantization bits
        safety_margin: Safety margin as fraction of available VRAM
        batch_size: Batch size for activation calculation (default: 1)
        seq_length: Sequence length for activation calculation (default: 2048)

    Returns:
        Dictionary containing the distribution plan
    """
    tour_distance = 0.0
    total_layers = config.get("num_hidden_layers")
    vram_per_layer_gb, embedding_vram_gb, total_model_vram_gb = estimate_layer_vram(
        config, q_bits, batch_size, seq_length, include_kv_cache=False
    )
    print(
        f"🔍 Model {config.get('model_name')} has {total_layers} layers and requires {total_model_vram_gb:.2f} GB of VRAM"
    )

    # Calculate effective VRAM for each peer (with safety margin)
    effective_vram = {
        peer_id: vram * (1 - safety_margin) for peer_id, vram in peers_vram.items()
    }

    # Calculate total effective VRAM across all peers
    total_effective_vram = sum(effective_vram.values())

    # Check if model can fit at all
    if total_effective_vram < total_model_vram_gb:
        print(
            f"⚠️ Warning: Total effective VRAM ({total_effective_vram:.2f}GB) "
            f"may be insufficient for model ({total_model_vram_gb:.2f}GB)"
        )

    # Find the peer with highest VRAM to handle embeddings
    embedding_peer = max(peers_vram.items(), key=lambda x: x[1])[0]

    # Calculate effective capacity in terms of layers for each peer
    peer_capacities = {}
    for peer_id, vram in effective_vram.items():
        if peer_id == embedding_peer:
            # This peer handles embeddings, so subtract that from capacity
            available_for_layers = vram - embedding_vram_gb
            max_layers = max(0, int(available_for_layers / vram_per_layer_gb))
        else:
            max_layers = int(vram / vram_per_layer_gb)
        peer_capacities[peer_id] = max_layers

    total_capacity = sum(peer_capacities.values())

    # If total capacity is less than total layers, we need to adjust
    if total_capacity < total_layers:
        print(
            f"⚠️ Total capacity ({total_capacity} layers) < model layers ({total_layers}). "
            f"Will distribute what's possible."
        )

    # Calculate proportional distribution based on capacity
    distribution = {}
    assigned_so_far = 0
    remaining_layers = total_layers

    # Sort peers by capacity for consistent assignment (largest capacity first)
    sorted_peers = sorted(peer_capacities.items(), key=lambda x: x[1], reverse=True)

    for i, (peer_id, capacity) in enumerate(sorted_peers):
        if remaining_layers <= 0:
            break

        # Calculate proportional share of remaining layers
        if i == len(sorted_peers) - 1:  # Last peer gets all remaining
            assigned_layers = min(capacity, remaining_layers)
        else:
            # Calculate this peer's proportion of total remaining capacity
            remaining_capacity = sum(cap for pid, cap in sorted_peers[i:])
            if remaining_capacity > 0:
                proportion = capacity / remaining_capacity
                target_layers = int(remaining_layers * proportion)
                # Ensure at least 1 layer if peer has capacity and layers remain
                if target_layers == 0 and capacity > 0 and remaining_layers > 0:
                    target_layers = 1
                assigned_layers = min(capacity, target_layers, remaining_layers)
            else:
                assigned_layers = 0

        if assigned_layers > 0:
            # Calculate actual VRAM usage
            is_embedding_peer = peer_id == embedding_peer
            vram_usage = (embedding_vram_gb if is_embedding_peer else 0) + (
                assigned_layers * vram_per_layer_gb
            )

            distribution[peer_id] = {
                "assigned_layers": assigned_layers,
                "handles_embeddings": is_embedding_peer,
                "available_vram_gb": peers_vram[peer_id],
                "estimated_vram_usage": round(vram_usage, 3),
                "vram_utilization_percent": round(
                    vram_usage / peers_vram[peer_id] * 100, 1
                ),
            }
            remaining_layers -= assigned_layers
            assigned_so_far += assigned_layers

    # Balance check: Try to equalize utilization percentages if there's significant imbalance
    if len(distribution) > 1:
        utilizations = [d["vram_utilization_percent"] for d in distribution.values()]
        max_util = max(utilizations)
        min_util = min(utilizations)

        # If there's more than 20% difference, try to rebalance
        if max_util - min_util > 20:
            print(
                f"🔄 Rebalancing distribution (util range: {min_util:.1f}% - {max_util:.1f}%)"
            )

            # Calculate target utilization (average)
            target_utilization = sum(utilizations) / len(utilizations)

            # Recalculate distribution based on target utilization
            new_distribution = {}
            remaining_layers = total_layers

            for peer_id, vram in sorted(
                peers_vram.items(), key=lambda x: x[1], reverse=True
            ):
                if remaining_layers <= 0:
                    break

                is_embedding_peer = peer_id == embedding_peer

                # Calculate target VRAM usage based on target utilization
                target_vram_usage = (
                    vram * (target_utilization / 100) * (1 - safety_margin)
                )

                # Subtract embeddings if this peer handles them
                if is_embedding_peer:
                    available_for_layers = target_vram_usage - embedding_vram_gb
                else:
                    available_for_layers = target_vram_usage

                # Calculate layers
                target_layers = max(0, int(available_for_layers / vram_per_layer_gb))
                assigned_layers = min(target_layers, remaining_layers)

                if assigned_layers > 0 or is_embedding_peer:
                    vram_usage = (embedding_vram_gb if is_embedding_peer else 0) + (
                        assigned_layers * vram_per_layer_gb
                    )

                    new_distribution[peer_id] = {
                        "assigned_layers": assigned_layers,
                        "handles_embeddings": is_embedding_peer,
                        "available_vram_gb": vram,
                        "estimated_vram_usage": round(vram_usage, 3),
                        "vram_utilization_percent": round(vram_usage / vram * 100, 1),
                    }
                    remaining_layers -= assigned_layers

            # Use rebalanced distribution if it assigned all layers
            if remaining_layers == 0:
                distribution = new_distribution
                print("✅ Rebalancing successful")
            else:
                print(
                    f"⚠️ Rebalancing left {remaining_layers} layers unassigned, keeping original"
                )

    # Final statistics
    total_assigned_layers = sum(
        peer["assigned_layers"] for peer in distribution.values()
    )
    embedding_assigned = any(
        peer["handles_embeddings"] for peer in distribution.values()
    )
    can_fit_model = total_assigned_layers >= total_layers and embedding_assigned

    # Summary metrics for the baseline (used in side-by-side comparison)
    tour = list(distribution.keys())
    avg_carbon_intensity = (sum(peers_carbon_intensity[pid] for pid in tour) / len(tour)) if tour else 0.0
    coords_dict = {pid: peers_locations[pid] for pid in tour if pid in peers_locations} 
    tour_distance = calculate_tour_distance(tour, coords_dict) if len(tour) > 1 else 0.0

    # Print distribution summary
    print("\n📊 Layer Distribution Summary (Traditional):")
    print(f"• Machines used: {len(distribution)}")
    print(f"• Total capacity: {total_capacity:.2f}GB")
    print(f"• Average local grid emissions: {avg_carbon_intensity:.1f} gCO₂/kWh")
    print(f"• Approx. total data travel between machines: {tour_distance:.1f} km")
    print("• Layers placed across machines to fit available memory:")

    for peer_id, info in distribution.items():
        print(
            f"   {peer_id}: {info['assigned_layers']} layers, "
            f"{info['vram_utilization_percent']:.1f}% VRAM utilization, "
            f"{peers_carbon_intensity[peer_id]:.1f} gCO2/kWh"
            f"{' (+ embeddings)' if info['handles_embeddings'] else ''}"
        )

    return {
        "distribution": distribution,
        "model_info": {
            "total_layers": total_layers,
            "total_assigned_layers": total_assigned_layers,
            "vram_per_layer_gb": round(vram_per_layer_gb, 3),
            "embedding_vram_gb": round(embedding_vram_gb, 3),
            "total_model_vram_gb": round(total_model_vram_gb, 3),
            "batch_size": batch_size,
            "seq_length": seq_length,
        },
        "can_fit_model": can_fit_model,
        "remaining_layers": total_layers - total_assigned_layers,
        "total_peers": len(peers_vram),
        "utilized_peers": len(distribution),
        "total_available_vram_gb": round(sum(peers_vram.values()), 3),
        "optimization_info": {
            "selected_peers": tour,
            "ordered_peers": tour,
            "avg_carbon_intensity": round(avg_carbon_intensity, 1),
            "tour_distance_km": round(tour_distance if len(tour) > 1 else 0.0, 1),
        },
    }

def optimized_distribute_layers_across_peers(
    config: Dict[str, Any],
    peers_vram: Dict[str, float],  # peer_id -> available_vram_gb
    peers_carbon_intensity: Dict[str, float],  # peer_id -> carbon intensity (gCO2/kWh)
    peers_locations: Dict[str, Tuple[float, float]],  # peer_id -> (lat, lon)
    q_bits: int,
    safety_margin: float = 0.1,
    batch_size: int = 1,
    seq_length: int = 2048,
    server_location: Tuple[float, float] = None,  # (lat, lon) of server/client ingress
) -> Dict[str, Any]:
    """
    Distribute model layers optimally across multiple GPU peers using a greedy + local refinement approach.
    Considers VRAM capacity, carbon emissions, and travel distance for optimal placement.

    Args:
        config: Model configuration dictionary
        peers_vram: Dictionary mapping peer_id to available VRAM in GB
        peers_carbon_intensity: Dictionary mapping peer_id to carbon intensity (gCO2/kWh)
        peers_locations: Dictionary mapping peer_id to (latitude, longitude) coordinates
        q_bits: Quantization bits
        safety_margin: Safety margin as fraction of available VRAM
        batch_size: Batch size for activation calculation (default: 1)
        seq_length: Sequence length for activation calculation (default: 2048)
        server_location: Optional (lat, lon) of server/client ingress point

    Returns:
        Dictionary containing the distribution plan with optimized peer ordering
    """
    tour_distance = 0.0
    total_layers = config.get("num_hidden_layers")
    vram_per_layer_gb, embedding_vram_gb, total_model_vram_gb = estimate_layer_vram(
        config, q_bits, batch_size, seq_length, include_kv_cache=False
    )
    print(
        f"🔍 Model {config.get('model_name')} has {total_layers} layers and requires {total_model_vram_gb:.2f} GB of VRAM"
    )

    # ============================================================================
    # A. SELECT PEERS (feasibility first, then quality)
    # ============================================================================
    
    # Build candidate list of active peers with free_vram_gb > 0 and known data
    candidate_peers = []
    for peer_id, vram in peers_vram.items():
        if vram > 0 and peer_id in peers_carbon_intensity and peer_id in peers_locations:
            candidate_peers.append(peer_id)
    
    if not candidate_peers:
        raise ValueError("No suitable peers found with VRAM, carbon intensity, and location data")
    
    # print(f"🌍 Found {len(candidate_peers)} candidate peers with complete data")
    
    # Calculate node scores (lower is better)
    node_scores = {}
    for peer_id in candidate_peers:
        carbon_intensity = peers_carbon_intensity[peer_id]
        vram_gb = peers_vram[peer_id]
        d_km = haversine_distance(server_location[0], server_location[1],
                                  peers_locations[peer_id][0], peers_locations[peer_id][1])
        node_scores[peer_id] = calculate_node_score(carbon_intensity, vram_gb, d_km)
    # Sort peers by node score (ascending - prefer lower emissions and higher VRAM)
    sorted_candidates = sorted(node_scores.items(), key=lambda x: x[1])
    # print(f"📊 Peer scores (lower is better): {[(pid, f'{score:.3f}') for pid, score in sorted_candidates[:5]]}")
    
    # Calculate effective VRAM for each peer (with safety margin)
    effective_vram = {
        peer_id: vram * (1 - safety_margin) for peer_id, vram in peers_vram.items()
    }
    
    # Greedy selection: pick peers in score order until we have enough capacity
    selected_peers = []
    total_capacity = 0
    required_vram = total_model_vram_gb
    
    for peer_id, _ in sorted_candidates:
        peer_capacity = effective_vram[peer_id]
        total_capacity += peer_capacity
        selected_peers.append(peer_id)
        
        if total_capacity >= required_vram:
            break
    
    # ============================================================================
    # B. ORDER PEERS TO MINIMIZE TRAVEL
    # ============================================================================
    
    if len(selected_peers) > 1:
        # Build coordinates list for selected peers
        coords = [(peer_id, peers_locations[peer_id][0], peers_locations[peer_id][1]) 
                  for peer_id in selected_peers]
        
        # Determine starting peer (closest to server or first peer)
        if server_location:
            server_lat, server_lon = server_location
            start_peer = min(coords, key=lambda x: haversine_distance(
                server_lat, server_lon, x[1], x[2]))[0]
        else:
            start_peer = selected_peers[0]  # Use first selected peer
        
        print(f"🚀 Starting travel optimization from peer: {start_peer}")
        
        # Run nearest-neighbor TSP
        initial_tour = nearest_neighbor_tsp(coords, start_peer)
        
        # Apply 2-opt improvement
        coords_dict = {peer_id: (lat, lon) for peer_id, lat, lon in coords}
        optimized_tour = two_opt_improvement(initial_tour, coords_dict)
        
        # Calculate tour distance
        tour_distance = calculate_tour_distance(optimized_tour, coords_dict)
        print(f"🗺️ Optimized peer order: {' → '.join(optimized_tour)}")
        
        # Use optimized order for layer distribution
        ordered_peers = optimized_tour
    else:
        ordered_peers = selected_peers
    
    # ============================================================================
    # C. DISTRIBUTE LAYERS ACROSS SELECTED AND ORDERED PEERS
    # ============================================================================
    
    # Find the peer with highest VRAM to handle embeddings (from selected peers)
    embedding_peer = max(selected_peers, key=lambda x: peers_vram[x])
    
    # Calculate effective capacity in terms of layers for each selected peer
    peer_capacities = {}
    for peer_id in selected_peers:
        vram = effective_vram[peer_id]
        if peer_id == embedding_peer:
            # This peer handles embeddings, so subtract that from capacity
            available_for_layers = vram - embedding_vram_gb
            max_layers = max(0, int(available_for_layers / vram_per_layer_gb))
        else:
            max_layers = int(vram / vram_per_layer_gb)
        peer_capacities[peer_id] = max_layers
    
    total_capacity = sum(peer_capacities.values())
    
    # Check if model can fit
    if total_capacity < total_layers:
        print(
            f"⚠️ Total capacity ({total_capacity} layers) < model layers ({total_layers}). "
            f"Will distribute what's possible."
        )
    
    # Distribute layers proportionally across selected peers
    distribution = {}
    remaining_layers = total_layers
    
    # Use ordered peers for distribution
    for i, peer_id in enumerate(ordered_peers):
        if remaining_layers <= 0:
            break
            
        capacity = peer_capacities[peer_id]
        
        # Calculate proportional share of remaining layers
        if i == len(ordered_peers) - 1:  # Last peer gets all remaining
            assigned_layers = min(capacity, remaining_layers)
        else:
            # Calculate this peer's proportion of total remaining capacity
            remaining_capacity = sum(peer_capacities[p] for p in ordered_peers[i:])
            if remaining_capacity > 0:
                proportion = capacity / remaining_capacity
                target_layers = int(remaining_layers * proportion)
                # Ensure at least 1 layer if peer has capacity and layers remain
                if target_layers == 0 and capacity > 0 and remaining_layers > 0:
                    target_layers = 1
                assigned_layers = min(capacity, target_layers, remaining_layers)
            else:
                assigned_layers = 0
        
        if assigned_layers > 0:
            # Calculate actual VRAM usage
            is_embedding_peer = peer_id == embedding_peer
            vram_usage = (embedding_vram_gb if is_embedding_peer else 0) + (
                assigned_layers * vram_per_layer_gb
            )
            
            # Get additional metadata
            carbon_intensity = peers_carbon_intensity[peer_id]
            lat, lon = peers_locations[peer_id]
            
            distribution[peer_id] = {
                "assigned_layers": assigned_layers,
                "handles_embeddings": is_embedding_peer,
                "available_vram_gb": peers_vram[peer_id],
                "estimated_vram_usage": round(vram_usage, 3),
                "vram_utilization_percent": round(
                    vram_usage / peers_vram[peer_id] * 100, 1
                ),
                "carbon_intensity": carbon_intensity,
                "location": {"lat": lat, "lon": lon},
                "node_score": node_scores[peer_id],
            }
            remaining_layers -= assigned_layers
    
    # Final statistics
    total_assigned_layers = sum(
        peer["assigned_layers"] for peer in distribution.values()
    )
    embedding_assigned = any(
        peer["handles_embeddings"] for peer in distribution.values()
    )
    can_fit_model = total_assigned_layers >= total_layers and embedding_assigned
    
    # Calculate average carbon intensity of selected peers
    avg_carbon_intensity = sum(peers_carbon_intensity[pid] for pid in selected_peers) / len(selected_peers)
    
    # Print distribution summary
    print("\n📊 Layer Distribution Summary (Optimized):")
    print(f"• Machines used: {len(selected_peers)}")
    print(f"• Total capacity: {total_capacity:.2f}GB")
    print(f"• Average local grid emissions: {avg_carbon_intensity:.1f} gCO₂/kWh")
    print(f"• Approx. total data travel between machines: {tour_distance:.1f} km")
    print("• Layers placed across machines to fit available memory:")
    
    for peer_id, info in distribution.items():
        print(
            f"   {peer_id}: {info['assigned_layers']} layers, "
            f"{info['vram_utilization_percent']:.1f}% VRAM, "
            f"{info['carbon_intensity']:.1f} gCO2/kWh"
            f"{' (+ embeddings)' if info['handles_embeddings'] else ''}"
        )

    return {
        "distribution": distribution,
        "model_info": {
            "total_layers": total_layers,
            "total_assigned_layers": total_assigned_layers,
            "vram_per_layer_gb": round(vram_per_layer_gb, 3),
            "embedding_vram_gb": round(embedding_vram_gb, 3),
            "total_model_vram_gb": round(total_model_vram_gb, 3),
            "batch_size": batch_size,
            "seq_length": seq_length,
        },
        "can_fit_model": can_fit_model,
        "remaining_layers": total_layers - total_assigned_layers,
        "total_peers": len(peers_vram),
        "utilized_peers": len(distribution),
        "total_available_vram_gb": round(sum(peers_vram.values()), 3),
        "optimization_info": {
            "selected_peers": selected_peers,
            "ordered_peers": ordered_peers,
            "avg_carbon_intensity": round(avg_carbon_intensity, 1),
            "tour_distance_km": round(tour_distance if len(selected_peers) > 1 else 0.0, 1),
            "node_scores": {pid: round(score, 3) for pid, score in node_scores.items()},
        },
    }
