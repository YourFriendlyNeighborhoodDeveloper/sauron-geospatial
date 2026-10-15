import argparse

from src.pipeline import Pipeline, run_demo

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m src")
    parser.add_argument("--demo", action="store_true", help="serve the dashboard from bundled fixtures (offline)")
    args = parser.parse_args()
    if args.demo:
        run_demo()
    else:
        Pipeline().run()
