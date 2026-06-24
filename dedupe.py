"""
Déduplication + clustering — le "cerveau" du système (version 4).

  - Embeddings (Gemini, gratuit) : présélection des clusters PROCHES,
    en ne comparant que le TITRE du message (pas l'emballage).
  - Arbitre IA (Groq, gratuit, ~14 400 appels/jour) : tranche
    "même événement ? OUI/NON" uniquement sur les cas proches.

Le LLM n'intervient donc que sur les cas ambigus. Règle d'or :
dans le doute, on sépare.
"""

import os
import re
import time
from datetime import datetime, timezone

from google import genai
from groq import Groq
from supabase import create_client

# --- Secrets (fournis par GitHub) ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Réglages ajustables ---
FENETRE_HEURES = 48          # on ne compare qu'aux clusters récents
TOP_K = 5                    # clusters candidats récupérés
SEUIL_CANDIDAT = 0.78        # en dessous : pas même un candidat
SEUIL_IDENTIQUE = 0.97       # au-dessus : quasi identique, rattacher sans LLM
MAX_CANDIDATS_LLM = 3        # candidats max soumis à l'arbitre
MESSAGES_PAR_PASSAGE = 40    # lot par passage
PAUSE_LLM = 2.1              # secondes entre 2 appels Groq (limite ~30/min)

MODELE_GROQ = "llama-3.1-8b-instant"

client_gemini = genai.Client(api_key=GEMINI_API_KEY)
client_groq = Groq(api_key=GROQ_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def nettoyer(texte):
    """Retire l'emballage : préfixes, liens, mentions."""
    texte = re.sub(r"https?://\S+", "", texte)
    texte = re.sub(r"\[[^\]]*\]\([^)]*\)", "", texte)
    texte = re.sub(r"(?i)^\s*intel\s*:", "", texte)
    texte = re.sub(r"(?i)\bjust in\b", "", texte)
    texte = re.sub(r"@\w+", "", texte)
    return texte.strip()


def extraire_titre(texte):
    """Garde l'essentiel : le titre / la première phrase, sans les listes."""
    t = nettoyer(texte)
    t = re.split(r"\n\s*(?:[-*•]|\d+[.)])\s", t)[0]
    t = t.split("\n\n")[0]
    return t.strip()[:300]


def vecteur_en_texte(v):
    return "[" + ",".join(str(x) for x in v) + "]"


def calculer_embedding(texte):
    reponse = client_gemini.models.embed_content(
        model="gemini-embedding-001",
        contents=texte,
        config={"task_type": "SEMANTIC_SIMILARITY", "output_dimensionality": 768},
    )
    return reponse.embeddings[0].values


def meme_evenement(texte_a, texte_b):
    """Demande à Groq si deux actualités décrivent le même événement précis.
    Lève une exception en cas d'indisponibilité (le message sera réessayé)."""
    prompt = (
        "Deux actualités crypto/finance. Décrivent-elles le MÊME événement "
        "précis (mêmes acteurs et même fait), et pas seulement un thème "
        f"proche ?\n\nA : {texte_a[:500]}\n\nB : {texte_b[:500]}\n\n"
        "Réponds uniquement par OUI ou NON."
    )
    reponse = client_groq.chat.completions.create(
        model=MODELE_GROQ,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=3,
    )
    return reponse.choices[0].message.content.strip().upper().startswith("OUI")


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

        # 2. Embedding du TITRE -> clusters candidats proches
        titre = extraire_titre(contenu)
        emb_txt = vecteur_en_texte(calculer_embedding(titre))
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
            # 3. Quasi identique -> rattacher sans déranger l'arbitre
            cible = candidats[0]["id"]
        elif candidats:
            # 4. Cas ambigus -> Groq tranche
            try:
                for c in candidats[:MAX_CANDIDATS_LLM]:
                    if meme_evenement(contenu, c["titre"] or ""):
                        cible = c["id"]
                        break
                    time.sleep(PAUSE_LLM)
            except Exception as e:
                print(f"  Arbitre indisponible, message reporté : {e}")
                reportes += 1
                continue

        if cible:
            supabase.table("messages").update(
                {"cluster_id": cible, "embedding": emb_txt}
            ).eq("id", msg_id).execute()
            supabase.table("clusters").update({"activite_le": maintenant()}).eq("id", cible).execute()
            rattaches += 1
        else:
            nouveau = (
                supabase.table("clusters")
                .insert({"titre": titre[:200], "centroide": emb_txt, "statut": "actif"})
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
