"""Analyze token classifier data readiness.

Usage:
    python -m scripts.analyze_classifier_data
"""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from edge.classifier.label_generator import (
    DEFAULT_WINDOW_DAYS,
    WINS_FOR_LEGIT,
    class_distribution,
    generate_labels,
)
from src.persistence.db import get_session_factory
from src.persistence.models import TokenFeatures, TokenOutcome


async def analyze() -> None:
    Session = get_session_factory()

    async with Session() as session:
        n_outcomes = (await session.execute(select(func.count()).select_from(TokenOutcome))).scalar()
        n_features = (await session.execute(select(func.count()).select_from(TokenFeatures))).scalar()
        n_with_tvl = (
            await session.execute(
                select(func.count()).where(TokenFeatures.tvl_usd.is_not(None))
            )
        ).scalar()
        n_with_holder = (
            await session.execute(
                select(func.count()).where(TokenFeatures.holder_count.is_not(None))
            )
        ).scalar()
        distinct_tokens = (
            await session.execute(
                select(func.count(TokenOutcome.token_address.distinct()))
            )
        ).scalar()

    print("=== Token Classifier Data Readiness ===")
    print(f"token_outcomes rows:           {n_outcomes}")
    print(f"distinct token addresses:      {distinct_tokens}")
    print(f"token_features rows:           {n_features}")
    print(f"  with tvl_usd:                {n_with_tvl}")
    print(f"  with holder_count:           {n_with_holder}")
    print(f"  missing rate tvl:            {1 - n_with_tvl / max(n_features, 1):.1%}")
    print(f"  missing rate holder:         {1 - n_with_holder / max(n_features, 1):.1%}")
    print()

    labeled = await generate_labels(window_days=DEFAULT_WINDOW_DAYS)
    dist = class_distribution(labeled)
    print(f"Labels (window={DEFAULT_WINDOW_DAYS}d, WINS_FOR_LEGIT={WINS_FOR_LEGIT}):")
    print(f"  legit:   {dist['legit']}")
    print(f"  scam:    {dist['scam']}")
    print(f"  unknown: {dist['unknown']}")
    trainable = dist["legit"] + dist["scam"]
    print(f"  => trainable samples: {trainable}")

    if trainable < 20:
        print("\n⚠️  CANNOT TRAIN — need at least 20 labeled samples")
    elif trainable < 100:
        print("\n⚠️  COLD START — RF with 2-win threshold recommended")
    else:
        print("\n✅  Ready to train")


def main() -> None:
    asyncio.run(analyze())


if __name__ == "__main__":
    main()
