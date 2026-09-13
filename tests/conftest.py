import os
import sys

if sys.platform == 'win32':
    original_unlink = os.unlink
    def patched_unlink(path):
        try:
            original_unlink(path)
        except PermissionError:
            if 'tmp' in path.lower() or 'temp' in path.lower():
                pass
            else:
                raise
    os.unlink = patched_unlink
