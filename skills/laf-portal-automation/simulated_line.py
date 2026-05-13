
import sys
import datetime

def send_line_notify(message):
    """
    Simulates sending a LINE notification to ADMIN.
    In production, this would use the LINE Notify API.
    Here, it prints to stdout and logs to a file.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_msg = f"[{timestamp}] [LINE-NOTIFY] To: ADMIN | Message: {message}"
    
    print(formatted_msg)
    
    # Log to a file for persistence/verification
    with open("line_notifications.log", "a") as f:
        f.write(formatted_msg + "\n")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        send_line_notify(sys.argv[1])
    else:
        print("Usage: python 06_simulated_line.py 'Message Content'")
