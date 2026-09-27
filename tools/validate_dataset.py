from __future__ import annotations

import argparse

from app import create_app
from app.extensions import db
from app.models import Meeting, Role, Segment, User
from app.services.importer import import_enriched_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Valida a importação completa e idempotente.")
    parser.add_argument("dataset")
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        user = User.query.filter_by(email="validation-admin@meetmind.local").first()
        if not user:
            user = User(
                email="validation-admin@meetmind.local",
                name="Validation Admin",
                role=Role.ADMIN,
            )
            user.set_password("validation-only-password-2026")
            db.session.add(user)
            db.session.commit()
        first = import_enriched_json(args.dataset, user)
        second = import_enriched_json(args.dataset, user)
        print("first", first)
        print("second", second)
        print("db_counts", {"meetings": Meeting.query.count(), "segments": Segment.query.count()})
        print(
            "approved_for_external_ai",
            Meeting.query.filter_by(approved_for_external_ai=True).count(),
        )


if __name__ == "__main__":
    main()
