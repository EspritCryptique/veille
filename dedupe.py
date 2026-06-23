"""
Déduplication + clustering — le "cerveau" du système.

À chaque passage :
  1. il prend les messages pas encore rangés dans un cluster ;
  2. (filtre 1, gratuit) si un message identique est déjà rangé, il le rattache ;
  3. (filtre 2) sinon il calcule l'embedding du message (Gemini) et cherche
     le cluster actif le plus proche ;
  4. si c'est assez proche -> même événement, il rattache ;
     sinon -> nouvel événement, il crée un nouveau cluster.

Principe : "dans le doute, on crée un nouveau cluster".
Aucun LLM ici : tout est déterministe + embeddings (quasi gratuit).
"""

import os
from datetime import datetime, timezone

from google import genai
from supabase import create_client

# --- Secrets (fournis par GitHub) ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Réglages que l'on ajustera en observant les résultats ---
SEUIL_SIMILARITE = 0.83     # au-dessus = "même événement"
FENETRE_HEURES = 48         # on ne compare qu'aux clusters récents
MESSAGES_PAR_PASSAGE = 50   # on traite par petits lots

client_gemini = genai.Client(api_key=GEMINI_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def vecteur_en_texte(v):
    """Format attendu par pgvector : [0.1,0.2,0.3,...]"""
    return "[" + ",".join(str(x) for x in v) + "]"


def calculer_embedding(texte):
    """Demande à Gemini la 'signature de sens' du texte (768 nombres)."""
    reponse = client_gemini.models.embed_content(
        model="gemini-embedding-001",
        contents=texte,
        config={"task_type": "SEMANTIC_SIMILARITY", "output_dimensionality": 768},
    )
    return reponse.embeddings[0].values


def maintenant():
    return datetime.now(timezone.utc).isoformat()


def main():
    # 1. Messages pas encore rangés (cluster_id vide)
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

    nouveaux, rattaches = 0, 0

    for msg in messages:
        msg_id = msg["id"]
        contenu = (msg["contenu"] or "").strip()
        if not contenu:
            continue

        # 2. Filtre 1 (gratuit) : un message identique est-il déjà rangé ?
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
            supabase.table("messages").update(
                {"cluster_id": cid_existant}
            ).eq("id", msg_id).execute()
            supabase.table("clusters").update(
                {"activite_le": maintenant()}
            ).eq("id", cid_existant).execute()
            rattaches += 1
            continue

        # 3. Filtre 2 : similarité de sens via embeddings
        emb_txt = vecteur_en_texte(calculer_embedding(contenu))

        proche = (
            supabase.rpc(
                "match_cluster",
                {"embedding_texte": emb_txt, "fenetre_heures": FENETRE_HEURES},
            )
            .execute()
            .data
        )

        if proche and proche[0]["similarite"] >= SEUIL_SIMILARITE:
            # Même événement -> rattacher au cluster existant
            cid = proche[0]["id"]
            supabase.table("messages").update(
                {"cluster_id": cid, "embedding": emb_txt}
            ).eq("id", msg_id).execute()
            supabase.table("clusters").update(
                {"activite_le": maintenant()}
            ).eq("id", cid).execute()
            rattaches += 1
        else:
            # Nouvel événement -> créer un cluster
            titre = contenu.replace("\n", " ")[:80]
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

    print(f"Terminé. {nouveaux} nouveaux clusters, {rattaches} rattachements.")


if __name__ == "__main__":
    main()
