"""
Memory-capped PDF text extraction, run as a SUBPROCESS by the worker.

The worker invokes:  python parse_subprocess.py <pdf_path>
and reads a single JSON line from stdout: {"text": "..."} or {"error": "..."}.

Running extraction in its own process with an RLIMIT_AS cap means a malicious or
pathological PDF can, at worst, kill this short-lived subprocess — never the
worker or the web server. On Linux the address-space limit is enforced by the
kernel; on macOS it is best-effort (dev only).
"""
import json
import os
import resource
import sys

# Cap this process's virtual address space. A 2-page CV needs a few MB; 512 MB
# is generous headroom while still stopping a decompression bomb from eating the
# whole box. Override with PARSE_MEM_LIMIT_MB.
MEM_LIMIT_MB = int(os.environ.get("PARSE_MEM_LIMIT_MB", "512"))


def _apply_memory_cap():
    try:
        limit = MEM_LIMIT_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        pass  # not enforceable on this platform; caller still has a timeout


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "no path"}))
        return 2
    _apply_memory_cap()
    path = sys.argv[1]
    try:
        with open(path, "rb") as f:
            raw = f.read()
        from pdftext import extract_text
        text = extract_text(raw)
        sys.stdout.write(json.dumps({"text": text}))
        return 0
    except MemoryError:
        sys.stdout.write(json.dumps({"error": "memory limit exceeded"}))
        return 1
    except Exception as e:
        sys.stdout.write(json.dumps({"error": str(e)[:200]}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
