from app.db import SessionLocal


def run() -> None:
    from app.seed import exercises, foods, habits, plans, recipes, settings

    db = SessionLocal()
    try:
        for mod in (habits, foods, recipes, exercises, plans, settings):
            name = mod.__name__.rsplit(".", 1)[-1]
            count = mod.seed(db)
            db.commit()
            print(f"seed {name}: {count} 条（已存在跳过）")
    finally:
        db.close()


if __name__ == "__main__":
    run()
