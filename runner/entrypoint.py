import os
import sys


def main() -> int:
    job_id = os.environ.get("JOB_ID", "")
    bridge_url = os.environ.get("BRIDGE_URL", "")
    print(
        f"sireum-runner stub: JOB_ID={job_id!r} BRIDGE_URL={bridge_url!r}",
        file=sys.stderr,
    )
    print("implement Phase 0 entrypoint (see runner/README.md)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
