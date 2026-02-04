import os
from pathlib import Path

from humor_reviews.humor import score_review
from humor_reviews.settings import ScoringSettings


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
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set in environment or .env")

    text = (
        "El señor mayor farmacéutico que me atendió muy prepotente y desagradable, "
        "sólo le faltó rebuznar. Aparte que toda la farmacia destila un aspecto de "
        "rancio y descuidado que tira para atrás.\n\n"
        "En definitiva, que pasan olímpicamente de ti y no te hacen ni caso.\n\n"
        "Suerte que les levanté un bote de Juanolas delante de sus narices y ni se enteraron..."
    )
    owner_reply = ""

    settings = ScoringSettings(
        provider="openai",
        model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        prompt=(
            "Evalua UNA resena individual y devuelve SOLO una puntuacion de humor.\n"
            "Devuelve un entero de 0 a 100 donde 0 es nada gracioso y 100 es muy gracioso.\n"
            "Prioriza resenas de una estrella si son graciosas.\n"
            "Nuestro humor es gamberro: insultos, situaciones dantescas y anecdotas graciosas.\n"
            "Si hay respuesta del propietario graciosa y no es copia y pega, sube la puntuacion.\n"
            "Si no hay nada gracioso, pon una puntuacion baja.\n"
            "No incluyas explicaciones ni texto extra.\n\n"
            "ESTRELLAS:\n{rating}\n\n"
            "RESENA:\n{review_text}\n\n"
            "RESPUESTA DEL PROPIETARIO:\n{owner_reply}"
        ),
        temperature=0.2,
        max_output_tokens=20,
    )

    result = score_review(text, owner_reply, 1, settings)
    print(result)


if __name__ == "__main__":
    main()
