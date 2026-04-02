
import subprocess
import logging

logger = logging.getLogger("ProcessCleaner")

def check_and_kill(port: int) -> bool:
    """
    Checks if a process is listening on the specified port and kills it.
    Returns True if a process was killed, False otherwise.
    """
    try:
        # Find PID using lsof
        logger.info("Checking port %d...", port)

        try:
            pid_output = subprocess.check_output(
                ["lsof", "-ti", f":{port}"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except subprocess.CalledProcessError:
            logger.info("Port %d is free.", port)
            return False

        if pid_output:
            pids = pid_output.split('\n')
            for p in pids:
                p = p.strip()
                if p and p.isdigit():
                    logger.warning("Process %s found on port %d. Force killing...", p, port)
                    subprocess.run(["kill", "-9", p], check=False)
                else:
                    logger.warning("Skipping invalid PID value: %r", p)

            logger.info("Port %d cleaned.", port)
            return True

    except Exception as e:
        logger.error("Error cleaning port %d: %s", port, e)
        return False
