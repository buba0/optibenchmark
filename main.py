import subprocess
import threading
import sys
import os
import time
from queue import Queue
import signal
import termios
import tty
import select

# Global flag for early termination
early_stop = threading.Event()

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
        process.send_signal(signal.SIGINT)
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

def run_iperf3(ip, duration, results, verbose=False, stop_event=None):
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
            if stop_event and stop_event.is_set():
                process.terminate()
                break
            line = line.strip()
            if verbose:
                print(f"[IPERF3] {line}")
            results.put(line)
    except KeyboardInterrupt:
        pass
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        # Collect any remaining output
        try:
            remaining = process.stdout.read()
            for line in remaining.split('\n'):
                line = line.strip()
                if line:
                    if verbose:
                        print(f"[IPERF3] {line}")
                    results.put(line)
        except:
            pass

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
    
    # If we don't have summary lines, try to extract from real-time output (last values)
    if not tx_sender or not rx_sender:
        # Look for the last real-time measurements
        tx_lines = [l for l in iperf_lines if "[TX-C]" in l and "sender" not in l.lower() and "receiver" not in l.lower()]
        rx_lines = [l for l in iperf_lines if "[RX-C]" in l and "sender" not in l.lower() and "receiver" not in l.lower()]
        
        # Get last TX measurement
        if tx_lines and not tx_sender:
            try:
                last_tx = tx_lines[-1].split()
                # Find Mbits/sec or Gbits/sec
                for i, part in enumerate(last_tx):
                    if "bits/sec" in part and i > 0:
                        tx_sender = f"{last_tx[i-1]} {part}"
                        tx_receiver = tx_sender  # Approximate
                        break
            except (IndexError, ValueError):
                pass
        
        # Get last RX measurement
        if rx_lines and not rx_sender:
            try:
                last_rx = rx_lines[-1].split()
                # Find Mbits/sec or Gbits/sec
                for i, part in enumerate(last_rx):
                    if "bits/sec" in part and i > 0:
                        rx_sender = f"{last_rx[i-1]} {part}"
                        rx_receiver = rx_sender  # Approximate
                        break
            except (IndexError, ValueError):
                pass
    
    result = []
    if tx_sender or tx_receiver:
        if tx_sender and tx_receiver:
            result.append(f"TX: {tx_sender} (sender), {tx_receiver} (receiver)")
        elif tx_sender:
            result.append(f"TX: {tx_sender} (estimated from last measurement)")
        elif tx_receiver:
            result.append(f"TX: {tx_receiver} (receiver)")
    
    if rx_sender or rx_receiver:
        if rx_sender and rx_receiver:
            result.append(f"RX: {rx_sender} (sender), {rx_receiver} (receiver)")
        elif rx_sender:
            result.append(f"RX: {rx_sender} (estimated from last measurement)")
        elif rx_receiver:
            result.append(f"RX: {rx_receiver} (receiver)")
    
    return "\n".join(result) if result else "No valid iperf3 results parsed."

def seconds_to_hms(seconds):
    """Convert seconds to a string in H:M:S format."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{int(h)}:{int(m):02}:{int(s):02}"

def check_for_key_press():
    """Check if 'q' key has been pressed (non-blocking)."""
    if select.select([sys.stdin], [], [], 0)[0]:
        key = sys.stdin.read(1)
        return key.lower() == 'q'
    return False

def progress_bar(duration, stop_event):
    """Display a simple progress bar for duration seconds, can be stopped early."""
    # Set terminal to raw mode to capture single key presses
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        
        for elapsed in range(duration):
            if stop_event.is_set():
                break
                
            # Check for 'q' key press
            if check_for_key_press():
                print("\n\n⚠️  Early stop requested (q pressed)...")
                stop_event.set()
                break
            
            bar_len = 30
            filled = int((elapsed+1)/duration * bar_len)
            percentage = int((elapsed+1)/duration * 100)
            remaining = duration - (elapsed + 1)
            elapsed_hms = seconds_to_hms(elapsed+1)
            remaining_hms = seconds_to_hms(remaining)
            bar = "=" * filled + "-" * (bar_len - filled)
            
            # Print progress bar with percentage and hint
            print(f"\r[{bar}] {percentage}% (Press 'q' to stop early)", end="", flush=True)
            # Move to next line and print time info
            print(f"\nElapsed: {elapsed_hms} | Remaining: {remaining_hms}", end="", flush=True)
            # Move cursor back up to overwrite both lines next iteration
            print("\033[F", end="", flush=True)
            
            time.sleep(1)
        
        if not stop_event.is_set():
            # Final output for normal completion
            print(f"\r[{'='*bar_len}] 100%                                ")
            print(f"Elapsed: {seconds_to_hms(duration)} | Remaining: {seconds_to_hms(0)}")
        else:
            # Clear the progress line
            print("\r" + " " * 80)
            print(" " * 80)
            print("\033[F\033[F", end="", flush=True)
    finally:
        # Restore terminal settings
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

def signal_handler(signum, frame):
    """Handle Ctrl-C gracefully."""
    print("\n\n⚠️  Early stop requested (Ctrl-C pressed)...")
    early_stop.set()

if __name__ == "__main__":
    verbose_mode = False
    args = sys.argv[1:]

    if "-v" in args:
        verbose_mode = True
        args.remove("-v")

    if len(args) != 2:
        print(f"Usage: {sys.argv[0]} [-v] <ip> <time>")
        print("  -v: verbose mode (show live output)")
        print("  Press 'q' or Ctrl-C during test to stop early and see results")
        sys.exit(1)

    ip = args[0]
    duration = int(args[1])

    # Set up signal handler for Ctrl-C
    signal.signal(signal.SIGINT, signal_handler)

    ping_results = Queue()
    iperf_results = Queue()

    ping_thread = threading.Thread(target=run_ping, args=(ip, ping_results, verbose_mode, early_stop), daemon=True)
    iperf_thread = threading.Thread(target=run_iperf3, args=(ip, duration, iperf_results, verbose_mode, early_stop))

    ping_thread.start()
    iperf_thread.start()

    if not verbose_mode:
        progress_bar(duration, early_stop)
    else:
        # In verbose mode, just wait for threads but still allow Ctrl-C
        try:
            while iperf_thread.is_alive():
                iperf_thread.join(timeout=0.5)
                if early_stop.is_set():
                    break
        except KeyboardInterrupt:
            print("\n\n⚠️  Early stop requested (Ctrl-C pressed)...")
            early_stop.set()

    # Wait for iperf to finish (it should finish quickly after early_stop is set)
    iperf_thread.join(timeout=3)

    # Track if this was an actual early stop
    was_early_stop = early_stop.is_set()
    
    # Always signal ping to stop and produce summary (whether early stop or normal completion)
    early_stop.set()
    
    # Give ping time to finish and output its summary
    time.sleep(1.0)

    if was_early_stop:
        print("\n⚠️  Test stopped early - showing partial results\n")
    else:
        print("\n✅ Test complete.\n")

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