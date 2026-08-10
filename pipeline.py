"""
Pipeline complet — enchaîne les quatre étapes dans une seule exécution.

Avant : quatre programmes séparés, réveillés chacun toutes les 5 minutes.
Les délais s'additionnaient (jusqu'à 20 minutes entre la publication d'une
news et l'arrivée de sa carte).

Maintenant : un seul passage fait tout, dans l'ordre. Une news collectée est
classée, rédigée et envoyée dans la foulée. Latence : 2 à 4 minutes.

Chaque étape est isolée : si l'une échoue, les suivantes s'exécutent quand
même, et l'étape en panne reprendra au passage suivant.
"""

import asyncio
import time
import traceback

import ingest
import dedupe
import redige
import cockpit


def etape(nom, fonction, est_async=False):
    """Exécute une étape en isolant ses erreurs des autres étapes."""
    print(f"\n=== {nom} ===")
    depart = time.time()
    try:
        if est_async:
            asyncio.run(fonction())
        else:
            fonction()
    except Exception:
        # On affiche l'erreur mais on continue : une panne ne bloque pas le reste
        print(f"!!! {nom} a échoué, on continue :")
        traceback.print_exc()
    print(f"--- {nom} terminé en {time.time() - depart:.0f} s")


def main():
    debut = time.time()

    etape("1. Ingestion Telegram", ingest.main, est_async=True)
    etape("2. Déduplication", dedupe.main)
    etape("3. Rédaction", redige.main)
    etape("4. Cockpit Telegram", cockpit.main)

    print(f"\nPipeline complet terminé en {time.time() - debut:.0f} s")


if __name__ == "__main__":
    main()
