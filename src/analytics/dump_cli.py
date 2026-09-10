import argparse

from analytics.dump import create_dump


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a controlled Analytics snapshot dump")
    parser.add_argument("--tenant", required=True, dest="tenant_id")
    parser.add_argument("--requested-by", default="github-actions")
    args = parser.parse_args()
    dump_id = create_dump(tenant_id=args.tenant_id, requested_by=args.requested_by)
    print(f"ANALYTICS_DUMP_COMPLETED={dump_id}")


if __name__ == "__main__":
    main()
