from src.persistence.models import ShadowSolution


def test_shadow_solution_has_feasibility_columns() -> None:
    cols = ShadowSolution.__table__.columns
    assert "feasible" in cols
    assert cols["feasible"].nullable is True
    assert "revert_reason" in cols
    assert cols["revert_reason"].nullable is True
