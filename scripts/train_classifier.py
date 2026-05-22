"""Train the token-quality classifier and (maybe) promote it.

Usage:
    python -m scripts.train_classifier --window-days 60
"""
from edge.classifier.train import main_cli

if __name__ == "__main__":
    main_cli()
