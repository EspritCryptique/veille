"""
Rédaction des drafts — transforme un cluster en post Telegram.

À chaque passage :
  1. il prend les clusters qui n'ont pas encore de brouillon Telegram ;
  2. il lit leurs messages sources (les FAITS) ;
  3. il demande à Groq de rédiger un post en français ;
  4. il enregistre le brouillon dans la table 'drafts' (statut 'en_attente').

Seule la rédaction (étape 3) utilise le LLM. Le reste est déterministe.
Anti-hallucination : le prompt interdit d'inventer chiffres et citations.
"""

import os
from datetime import datetime, timezone

from groq import Groq
from supabase import create_client

# --- Secrets (fournis par GitHub) ---
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Réglages ajustables ---
MODELE_GROQ = "llama-3.3-70b-versatile"   # modèle de meilleure qualité pour l'écriture
CLUSTERS_PAR_PASSAGE = 10                  # nombre de drafts rédigés par passage
MESSAGES_PAR_CLUSTER = 5                   # faits max transmis au LLM

# --- Charte éditoriale : MODIFIE ce texte pour imposer TON style ---
CHARTE_EDITORIALE = """
Tu écris pour un média crypto francophone, sur sa chaîne Telegram.
Ton : dynamique, clair, accessible, sérieux mais pas pompeux.
Format : court (2 à 4 phrases), une accroche forte dès le début,
éventuellement 1 ou 2 puces si utile. Un emoji pertinent en tête si approprié.
Termine par une courte mise en perspective si c'est pertinent.
Évite les superlatifs creux et le jargon inutile.
"""

client_groq = Groq(api_key=GROQ_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def maintenant():
    return datetime.now(timezone.utc).isoformat()


def rediger(faits):
    """Demande à Groq de rédiger un post Telegram à partir des faits fournis."""
    prompt = (
        f"{CHARTE_EDITORIALE}\n\n"
        "RÈGLE ABSOLUE : utilise UNIQUEMENT les faits ci-dessous. N'invente "
        "aucun chiffre, aucune citation, aucune date, aucun détail. Si une "
        "information manque, ne la mentionne pas.\n\n"
        "Rédige en FRANÇAIS un post Telegram à partir de ces faits :\n\n"
        f"{faits}\n\n"
        "Réponds uniquement par le texte du post, sans préambule ni explication."
    )
    reponse = client_groq.chat.completions.create(
        model=MODELE_GROQ,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6,
        max_tokens=400,
    )
    return reponse.choices[0].message.content.strip()


def main():
    # 1. Clusters ayant déjà un brouillon Telegram (à ne pas refaire)
    drafts_existants = (
        supabase.table("drafts").select("cluster_id").eq("reseau", "telegram").execute().data
    )
    deja_fait = {d["cluster_id"] for d in drafts_existants}

    # 2. Clusters récents, on garde ceux sans brouillon
    clusters = (
        supabase.table("clusters")
        .select("id, titre")
        .eq("statut", "actif")
        .order("cree_le", desc=True)
        .limit(60)
        .execute()
        .data
    )
    a_rediger = [c for c in clusters if c["id"] not in deja_fait][:CLUSTERS_PAR_PASSAGE]

    if not a_rediger:
        print("Aucun cluster à rédiger.")
        return

    ecrits = 0
    for cluster in a_rediger:
        cid = cluster["id"]

        # 3. Récupérer les faits (messages sources du cluster)
        msgs = (
            supabase.table("messages")
            .select("contenu")
            .eq("cluster_id", cid)
            .limit(MESSAGES_PAR_CLUSTER)
            .execute()
            .data
        )
        faits = "\n\n".join((m["contenu"] or "")[:500] for m in msgs if m["contenu"])
        if not faits.strip():
            continue

        # 4. Rédiger via Groq
        try:
            texte = rediger(faits)
        except Exception as e:
            print(f"  Rédaction indisponible pour {cid}, on réessaiera : {e}")
            continue

        # 5. Enregistrer le brouillon
        supabase.table("drafts").insert(
            {"cluster_id": cid, "reseau": "telegram", "contenu": texte, "statut": "en_attente"}
        ).execute()
        ecrits += 1

    print(f"Terminé. {ecrits} drafts rédigés.")


if __name__ == "__main__":
    main()
