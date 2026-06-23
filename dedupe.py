"""
Déduplication + clustering — le "cerveau" du système (version 2).

Logique :
  1. (filtre 1, gratuit) doublon exact déjà rangé -> rattacher ;
  2. embeddings (Gemini, gratuit) -> présélection des clusters PROCHES ;
  3. si un candidat est quasi identique -> rattacher directement ;
  4. sinon, sur les candidats proches uniquement, on demande à Gemini
     "même événement précis ? OUI/NON" -> le premier OUI gagne ;
  5. aucun candidat retenu -> nouvel événement (nouveau cluster).

Le LLM n'intervient QUE sur les cas ambigus (proches mais pas identiques).
Tout le reste est déterministe. Règle d'or : dans le doute, on sépare.
"""

import os
import re
from datetime import datetime, timezone

from google import genai
from supabase import create_client

# --- Secrets (fournis par GitHub) ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Réglages ajustables ---
FENETRE_HEURES = 48          # on ne compare qu'aux clusters récents
TOP_K = 5                    # nombre de clusters candidats récupérés
SEUIL_CANDIDAT = 0.78        # en dessous : pas même un candidat
SEUIL_IDENTIQUE = 0.97       # au-dessus : quasi identique, on rattache sans LLM
MAX_CANDIDATS_LLM = 3        # nombre max de candidats soumis au LLM
MESSAGES_PAR_PASSAGE = 40    # on traite par petits lots

MODELE_LLM = "gemini-2.5-flash-lite"

client_gemini = genai.Client(api_key=GEMINI_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def nettoyer_pour_embedding(texte):
    """Retire l'emballage (préfixes, liens, signatures) pour comparer le fond."""
    texte = re.sub(r"https?://\S+", "", texte)          # liens
    texte = re.sub(r"\[[^\]]*\]\([^)]*\)", "", texte)    # liens markdown [texte](url)
    texte = re.sub(r"(?i)^\s*intel\s*:", "", texte)      # préfixe "INTEL:"
    texte = re.sub(r"(?i)\bjust in\b", "", texte)        # "JUST IN"
    texte = re.sub(r"@\w+", "", texte)                    # mentions @
    return texte.strip()


def vecteur_en_texte(v):
    return "[" + ",".join(str(x) for x in v) + "]"


def calculer_embedding(texte):
    """Demande à Gemini la 'signature de sens' du texte (768 nombres)."""
    reponse = client_gemini.models.embed_content(
        model="gemini-embedding-001",
        contents=nettoyer_pour_embedding(texte),
        config={"task_type": "SEMANTIC_SIMILARITY", "output_dimensionality": 768},
    )
    return reponse.embeddings[0].values


def meme_evenement(texte_a, texte_b):
    """Demande à Gemini si deux actualités décrivent le même événement précis.
    Lève une exception en cas d'indisponibilité (le message sera réessayé plus tard).
    """
    prompt = (
        "Deux actualités crypto/finance. Décrivent-elles le MÊME événement "
        "précis (mêmes acteurs et même fait), et pas seulement un thème proche ?\n\n"
        f"A : {texte_a[:500]}\n\n"
        f"B : {texte_b[:500]}\n\n"
        "Réponds uniquement par OUI ou NON."
    )
    reponse = client_gemini.models.generate_content(model=MODELE_LLM, contents=prompt)
    return (reponse.text or "").strip().upper().startswith("OUI")


def maintenant():
    return datetime.now(timezone.utc).isoformat()


def main():
    messages = (
        supabase.table("messages")
        .select("id, contenu, hash")
        .filter("cluster_id", "is", "null")
        .order("poste_le", desc=False)
        .limit(MESSAGES_PAR_PASSAGE)
        .execute()
        .data
    )
    if not messages:
        print("Aucun nouveau message à traiter.")
        return

    nouveaux, rattaches, reportes = 0, 0, 0

    for msg in messages:
        msg_id = msg["id"]
        contenu = (msg["contenu"] or "").strip()
        if not contenu:
            continue

        # 1. Filtre 1 (gratuit) : doublon exact déjà rangé ?
        memes = (
            supabase.table("messages")
            .select("cluster_id")
            .eq("hash", msg["hash"])
            .limit(20)
            .execute()
            .data
        )
        cid_existant = next((m["cluster_id"] for m in memes if m["cluster_id"]), None)
        if cid_existant:
            supabase.table("messages").update({"cluster_id": cid_existant}).eq("id", msg_id).execute()
            supabase.table("clusters").update({"activite_le": maintenant()}).eq("id", cid_existant).execute()
            rattaches += 1
            continue

        # 2. Embeddings -> clusters candidats proches
        emb_txt = vecteur_en_texte(calculer_embedding(contenu))
        candidats = (
            supabase.rpc(
                "match_clusters",
                {"embedding_texte": emb_txt, "fenetre_heures": FENETRE_HEURES, "k": TOP_K},
            )
            .execute()
            .data
        )
        candidats = [c for c in candidats if c["similarite"] >= SEUIL_CANDIDAT]

        cible = None
        if candidats and candidats[0]["similarite"] >= SEUIL_IDENTIQUE:
            # 3. Quasi identique -> rattacher sans déranger le LLM
            cible = candidats[0]["id"]
        elif candidats:
            # 4. Cas ambigus -> on demande à Gemini de trancher
            try:
                for c in candidats[:MAX_CANDIDATS_LLM]:
                    if meme_evenement(contenu, c["titre"] or ""):
                        cible = c["id"]
                        break
            except Exception as e:
                # LLM indisponible (ex. limite atteinte) : on réessaiera plus tard
                print(f"  LLM indisponible, message reporté : {e}")
                reportes += 1
                continue

        if cible:
            supabase.table("messages").update(
                {"cluster_id": cible, "embedding": emb_txt}
            ).eq("id", msg_id).execute()
            supabase.table("clusters").update({"activite_le": maintenant()}).eq("id", cible).execute()
            rattaches += 1
        else:
            # 5. Nouvel événement
            titre = contenu.replace("\n", " ")[:200]
            nouveau = (
                supabase.table("clusters")
                .insert({"titre": titre, "centroide": emb_txt, "statut": "actif"})
                .execute()
                .data
            )
            cid = nouveau[0]["id"]
            supabase.table("messages").update(
                {"cluster_id": cid, "embedding": emb_txt}
            ).eq("id", msg_id).execute()
            nouveaux += 1

    print(f"Terminé. {nouveaux} nouveaux clusters, {rattaches} rattachements, {reportes} reportés.")


if __name__ == "__main__":
    main()
