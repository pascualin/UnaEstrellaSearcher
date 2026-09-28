import os
from pathlib import Path

from humor_reviews.humor import score_review
from humor_reviews.settings import load_settings


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def main() -> None:
    _load_env(Path(".env"))
    settings = load_settings()
    if not os.getenv(settings.scoring.api_key_env):
        raise SystemExit(
            f"{settings.scoring.api_key_env} not set in environment or .env"
        )

    text = (
        "El señor mayor farmacéutico que me atendió muy prepotente y desagradable, "
        "sólo le faltó rebuznar. Aparte que toda la farmacia destila un aspecto de "
        "rancio y descuidado que tira para atrás.\n\n"
        "En definitiva, que pasan olímpicamente de ti y no te hacen ni caso.\n\n"
        "Suerte que les levanté un bote de Juanolas delante de sus narices y ni se enteraron..."
    )
    owner_reply = ""

    result = score_review(text, owner_reply, 1, settings.scoring)
    print(result)


if __name__ == "__main__":
    main()
