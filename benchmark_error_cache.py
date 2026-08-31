import time
import sys
from pathlib import Path

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent))

from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.models import RankingMode

def run_benchmark(engine_name: str, engine: AutocompleteSystem, typo_query: str):
    print(f"\n{'='*40}")
    print(f"Benchmarking {engine_name} Engine")
    print(f"{'='*40}")
    
    # 1. First Organic Search (No Cache)
    start_time = time.perf_counter()
    results_organic = engine.get_ranked_completions(typo_query, k=5)
    end_time = time.perf_counter()
    organic_latency = (end_time - start_time) * 1000
    
    print(f"Phase 1: Organic Search (Uncached)")
    print(f"  Latency: {organic_latency:.2f} ms")
    if results_organic:
        print(f"  Top Match: '{results_organic[0][1].completed_sentence[:50]}...'")
    else:
        print("  No matches found!")
        
    # Check if the error cache successfully queued the correction
    queued_items = engine.error_cache.queue
    print(f"  Queue state after search: {queued_items}")
    
    # 2. Trigger the asynchronous background worker
    print("\nTriggering ErrorCache Background Worker...")
    engine.error_cache.rebuild_cycle()
    print(f"  Active cache state: {engine.error_cache.cache}")
    
    # 3. Second Search (Fast Path via Aho-Corasick)
    start_time = time.perf_counter()
    results_cached = engine.get_ranked_completions(typo_query, k=5)
    end_time = time.perf_counter()
    cached_latency = (end_time - start_time) * 1000
    
    print(f"\nPhase 2: Aho-Corasick Fast Path (Cached)")
    print(f"  Latency: {cached_latency:.2f} ms")
    if results_cached:
        print(f"  Top Match: '{results_cached[0][1].completed_sentence[:50]}...'")
    
    # Calculate speedup
    speedup = organic_latency / cached_latency if cached_latency > 0 else 0
    print(f"\nResult: The Error Cache provided a {speedup:.1f}x speedup for {engine_name}!")

if __name__ == "__main__":
    print("Loading indexes from disk...")
    # Load SQLite System
    try:
        sqlite_engine = AutocompleteSystem.load(Path("data/data_sqlite"))
    except FileNotFoundError:
        print("Error: data/data_sqlite not found. Please run build_index.py first.")
        sys.exit(1)
        
    # Load Array System
    try:
        array_engine = AutocompleteSystem.load(Path("data/data_array"))
    except FileNotFoundError:
        print("Error: data/data_array not found.")
        sys.exit(1)
        
    print("Indexes loaded successfully.")

    # We use a typo that will realistically map to something in the c-api corpus
    # e.g., 'pythun' for 'python', or 'integdr' for 'integer'
    test_query = "pythun"
    
    run_benchmark("Suffix Array", array_engine, test_query)
    run_benchmark("SQLite", sqlite_engine, test_query)
