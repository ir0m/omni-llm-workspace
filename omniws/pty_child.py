"""Establish a controlling terminal in the new session, then replace this process with bash."""
import fcntl
import os
import termios

if __name__ == "__main__":
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execv("/bin/bash", ["bash", "--noprofile", "--norc", "-i"])
