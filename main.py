import subprocess
import threading
import sys
import os
import time
from queue import Queue

def run_ping(ip, results, verbose=False, stop_event=None):
    """Run ping with 0.2s interval and collect results."""
    process = subprocess.Popen(
        ["ping", ip, "-i", "0.2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    try:
        for line in process.stdout:
            if stop_event and stop_event.is_set():
                break
            line = line.strip()
            if verbose:
                print(f"[PING] {line}")
            results.put(line)
    except KeyboardInterrupt:
        pass
    finally:
        # Send SIGINT instead of SIGTERM to allow ping to print statistics
        process.send_signal(subprocess.signal.SIGINT)
        # Wait a bit for the summary to be printed
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
        # Collect any remaining output (the summary)
        try:
            remaining = process.stdout.read()
            for line in remaining.split('\n'):
                line = line.strip()
                if line:
                    if verbose:
                        print(f"[PING] {line}")
                    results.put(line)
        except:
            pass

def run_iperf3(ip, duration, results, verbose=False):
    """Run iperf3 bidirectional test and collect results."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    process = subprocess.Popen(
        ["stdbuf", "-oL", "-eL", "iperf3", "-c", ip, "--bidir", "-t", str(duration)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env
    )
    try:
        for line in process.stdout:
            line = line.strip()
            if verbose:
                print(f"[IPERF3] {line}")
            results.put(line)
    except KeyboardInterrupt:
        pass
    finally:
        process.wait()

def summarize_ping(ping_lines):
    """Extract packet statistics and latency from Linux ping output."""
    transmitted = received = loss = None
    min_latency = avg_latency = max_latency = None

    for line in ping_lines:
        line_lower = line.lower()
        if "packets transmitted" in line_lower:
            try:
                parts = [p.strip() for p in line.split(",")]
                transmitted = parts[0].split()[0]
                received = parts[1].split()[0]
                loss = parts[2].split("%")[0].split()[-1]
            except (IndexError, ValueError):
                pass
        elif "rtt min/avg/max" in line_lower or "round-trip min/avg/max" in line_lower:
            try:
                stats = line.split("=")[1].strip().split("/")
                min_latency = stats[0].strip()
                avg_latency = stats[1].strip()
                max_latency = stats[2].strip().split()[0]  # Remove unit if present
            except (IndexError, ValueError):
                pass

    if transmitted and received and loss:
        result = []
        result.append(f"Transmitted: {transmitted}")
        result.append(f"Received: {received}")
        
        # Calculate lost packet count
        try:
            lost_count = int(transmitted) - int(received)
            result.append(f"Lost: {lost_count}")
        except (ValueError, TypeError):
            pass
        
        result.append(f"Packet Loss: {loss}%")
        result.append(f"Minimum RTT: {min_latency} ms" if min_latency else "Minimum RTT: N/A")
        result.append(f"Maximum RTT: {max_latency} ms" if max_latency else "Maximum RTT: N/A")
        result.append(f"Average RTT: {avg_latency} ms" if avg_latency else "Average RTT: N/A")
        return "\n".join(result)
    else:
        return "No valid ping summary found."

def summarize_iperf(iperf_lines):
    """Extract iperf3 sender/receiver results with cleaner formatting."""
    summary_lines = [l for l in iperf_lines if "sender" in l.lower() or "receiver" in l.lower()]
    if not summary_lines:
        return "No iperf3 summary found."
    
    # Parse TX and RX results
    tx_sender = tx_receiver = rx_sender = rx_receiver = None
    
    for line in summary_lines:
        parts = line.split()
        if len(parts) < 7:
            continue
        
        # Extract bitrate (typically second to last value before "Mbits/sec" or "Gbits/sec")
        try:
            # Find the position of "sender" or "receiver"
            if "sender" in line.lower():
                is_sender = True
            else:
                is_sender = False
            
            # Look for bitrate value
            bitrate_idx = -3  # Usually: ... XXX Mbits/sec sender/receiver
            bitrate = parts[bitrate_idx]
            unit = parts[bitrate_idx + 1]
            
            if "[TX-C]" in line or "TX-C" in line:
                if is_sender:
                    tx_sender = f"{bitrate} {unit}"
                else:
                    tx_receiver = f"{bitrate} {unit}"
            elif "[RX-C]" in line or "RX-C" in line:
                if is_sender:
                    rx_sender = f"{bitrate} {unit}"
                else:
                    rx_receiver = f"{bitrate} {unit}"
        except (IndexError, ValueError):
            continue
    
    result = []
    if tx_sender and tx_receiver:
        result.append(f"TX: {tx_sender} (sender), {tx_receiver} (receiver)")
    if rx_sender and rx_receiver:
        result.append(f"RX: {rx_sender} (sender), {rx_receiver} (receiver)")
    
    return "\n".join(result) if result else "No valid iperf3 results parsed."

def seconds_to_hms(seconds):
    """Convert seconds to a string in H:M:S format."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{int(h)}:{int(m):02}:{int(s):02}"

def progress_bar(duration):
    """Display a simple progress bar for duration seconds."""
    for elapsed in range(duration):
        bar_len = 30
        filled = int((elapsed+1)/duration * bar_len)
        percentage = int((elapsed+1)/duration * 100)
        remaining = duration - (elapsed + 1)
        elapsed_hms = seconds_to_hms(elapsed+1)
        remaining_hms = seconds_to_hms(remaining)
        bar = "=" * filled + "-" * (bar_len - filled)
        
        # Print progress bar with percentage
        print(f"\r[{bar}] {percentage}%", end="", flush=True)
        # Move to next line and print time info
        print(f"\nElapsed: {elapsed_hms} | Remaining: {remaining_hms}", end="", flush=True)
        # Move cursor back up to overwrite both lines next iteration
        print("\033[F", end="", flush=True)
        
        time.sleep(1)
    
    # Final output
    print(f"\r[{'='*bar_len}] 100%")
    print(f"Elapsed: {seconds_to_hms(duration)} | Remaining: {seconds_to_hms(0)}")
    
if __name__ == "__main__":
    verbose_mode = False
    args = sys.argv[1:]

    if "-v" in args:
        verbose_mode = True
        args.remove("-v")

    if len(args) != 2:
        print(f"Usage: {sys.argv[0]} [-v] <ip> <time>")
        sys.exit(1)

    ip = args[0]
    duration = int(args[1])

    ping_results = Queue()
    iperf_results = Queue()
    stop_event = threading.Event()

    ping_thread = threading.Thread(target=run_ping, args=(ip, ping_results, verbose_mode, stop_event), daemon=True)
    iperf_thread = threading.Thread(target=run_iperf3, args=(ip, duration, iperf_results, verbose_mode))

    ping_thread.start()
    iperf_thread.start()

    if not verbose_mode:
        progress_bar(duration)

    iperf_thread.join()

    # Signal ping to stop and give it time to finish
    stop_event.set()
    time.sleep(0.5)

    print("\nTest complete.\n")

    # Collect results
    ping_lines = []
    while not ping_results.empty():
        ping_lines.append(ping_results.get())

    iperf_lines = []
    while not iperf_results.empty():
        iperf_lines.append(iperf_results.get())

    # Print summary
    print("=== Summary ===")
    print("📡 Ping:")
    for line in summarize_ping(ping_lines).split('\n'):
        print("  " + line)
    print("\n🚀 iPerf3:")
    for line in summarize_iperf(iperf_lines).split('\n'):
        print("  " + line)