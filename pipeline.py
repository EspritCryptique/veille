"""
Pipeline complet — enchaîne les quatre étapes dans une seule exécution.

Chaque étape est TOTALEMENT isolée : son module n'est chargé qu'au moment
de l'exécuter. Si un fichier est cassé ou incomplet, seule son étape échoue,
les autres continuent normalement. Une rédaction en panne n'empêche donc pas
la collecte, ni le traitement de tes clics dans le cockpit.
"""

import asyncio
import importlib
import time
import traceback


def etape(nom, module, est_async=False):
    """Charge le module puis exécute sa fonction main(), en isolant les erreurs."""
    print(f"\n=== {nom} ===")
    depart = time.time()
    try:
        mod = importlib.import_module(module)   # chargement tardif = isolation réelle
        if est_async:
            asyncio.run(mod.main())
        else:
            mod.main()
    except Exception:
        print(f"!!! {nom} a échoué, on continue avec les étapes suivantes :")
        traceback.print_exc()
    print(f"--- {nom} terminé en {time.time() - depart:.0f} s")


def main():
    debut = time.time()

    etape("1. Ingestion Telegram", "ingest", est_async=True)
    etape("2. Déduplication", "dedupe")
    etape("3. Rédaction", "redige")
    etape("4. Cockpit Telegram", "cockpit")

    print(f"\nPipeline complet terminé en {time.time() - debut:.0f} s")


if __name__ == "__main__":
    main()
