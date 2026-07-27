"""
Rédaction des drafts — transforme un cluster en post Telegram.

À chaque passage :
  1. il prend les clusters qui n'ont pas encore de brouillon Telegram ;
  2. il lit leurs messages sources (les FAITS) ;
  3. il demande à Groq de rédiger un post en français, selon la charte ;
  4. il enregistre le brouillon dans la table 'drafts' (statut 'en_attente').

Seule la rédaction (étape 3) utilise le LLM. Le reste est déterministe.
Anti-hallucination : le prompt interdit d'inventer chiffres et citations.
"""

import os
from datetime import datetime, timedelta, timezone

from groq import Groq
from supabase import create_client

# --- Secrets (fournis par GitHub) ---
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Réglages ajustables ---
MODELE_GROQ = "openai/gpt-oss-120b"        # modèle de meilleure qualité pour l'écriture
CLUSTERS_PAR_PASSAGE = 10                   # nombre de drafts rédigés par passage
MESSAGES_PAR_CLUSTER = 5                    # faits max transmis au LLM
AGE_MAX_HEURES = 24                         # on ne rédige que pour l'actu fraîche

# --- CHARTE ÉDITORIALE : c'est ici que vit TON style. Modifie librement. ---
CHARTE_EDITORIALE = """
Tu écris un post pour la chaîne Telegram d'un média crypto francophone.

OBJECTIF : transmettre l'information principale de façon complète, mais la plus
claire et concise possible.

LONGUEUR ET STRUCTURE :
- Vise UNE seule phrase courte si l'essentiel peut être dit ainsi.
- Passe à DEUX phrases seulement si l'information doit être complétée ou précisée.
  Jamais plus de deux phrases, et toujours moins de 280 caractères au total.
- Termine toujours chaque phrase par un point.
- Écris à la VOIX ACTIVE, avec l'acteur en sujet quand il y en a un.
  Écris "Strategy annonce un rachat de 25 millions $ de ses actions", jamais
  "25 millions $ de rachat d'actions a été déclaré par Strategy".
  Écris "Les ETF Bitcoin spot américains ont enregistré 33,79 millions $ d'entrées
  nettes", jamais "33,79 millions $ d'entrées nettes ont afflué dans les ETF".
- Place le chiffre en tête de phrase UNIQUEMENT s'il n'y a pas d'acteur identifiable
  (ex. "100 millions $ de positions shorts ont été liquidées").

TON ET LANGUE :
- Ton neutre et journalistique : aucun commentaire, aucune opinion.
- Phrases courtes et factuelles. Conserve les chiffres précis des faits.
- Choisis toujours le verbe et la tournure les plus naturels et concis en français.
  Écris "dominer" plutôt que "prendre la tête de".
- Nomme les personnalités par leur prénom ET leur nom, SANS titre honorifique.
  Écris "Donald Trump", jamais "le président Donald Trump" ni "Trump" seul.
- PRÉCISE LA NATURE de l'entité quand le nom seul serait ambigu :
  "L'exchange BitMart", "L'action Google", "Le cours du pétrole Brent".
- Supprime les tickers boursiers accolés au nom d'une société.
  Écris "L'action SpaceX", jamais "SpaceX $SPCX" ni "Google $GOOGL".
- N'emploie JAMAIS le possessif anglais en 's. Garde le nom seul, ou tourne
  la phrase en français. Écris "Strategy", jamais "Michael Saylor's Strategy".
- Évite les calques de l'anglais : préfère la tournure française naturelle.
  Écris "ne devrait pas réduire", jamais "n'est pas prévue pour réduire".
- N'abrège pas les noms de pays. Écris "entre les États-Unis et l'Iran",
  jamais "pourparlers US-Iran".
- Préfère une période relative à une date brute quand le fait est récent :
  "la semaine dernière" plutôt que "entre le 20 et le 26 juillet".
- Soigne l'orthographe française : "cessez-le-feu", "actions préférentielles".
- Si l'information n'est pas confirmée à 100 %, emploie le conditionnel.

TEMPS :
- Emploie le PRÉSENT de narration quand l'événement se produit ou s'annonce
  maintenant (ex. "La plateforme lance un produit") : plus vivant, "en direct".
- Mais emploie le PASSÉ COMPOSÉ quand le fait s'inscrit dans une fenêtre de temps
  révolue (ex. "dans les 60 dernières minutes", "hier", "la semaine dernière").

NOMS PROPRES ET ANGLICISMES (règle stricte) :
- Ne traduis JAMAIS le nom d'une loi, d'une entreprise ou d'un produit.
  Écris "le Clarity Act", jamais "l'acte Clarity".
- MAIS emploie le nom français d'usage des institutions quand il existe.
  Écris "la Réserve fédérale américaine" pour "the Federal Reserve",
  "le Département de la Justice américain" pour "the Department of Justice".
- N'emploie jamais de pluriel anglais : écris "les ETF", jamais "les ETFs".
- Mets la préposition devant les unités : "8 millions d'ETH", pas "8 millions ETH".
- Conserve les anglicismes courants du vocabulaire crypto et finance :
  short, long, staking, airdrop, trading, spot, hack, stablecoin, token...
  Écris "positions shorts", jamais "positions courtes".
- Ne traduis QUE les anglicismes rares, par le mot français le plus adapté
  (traduction par le sens, non littérale).
- N'explique un terme QUE s'il est rare et incompréhensible pour un non-initié.
  N'explique jamais les termes courants. N'en abuse pas.
- Ajoute un court élément de contexte UNIQUEMENT si un lecteur qui n'a pas suivi
  l'affaire ne pourrait pas comprendre. Reste sobre, sans en abuser.

CHIFFRES (règle stricte) :
- Écris les grands nombres avec l'unité en toutes lettres, format "13 millions $"
  ou "1 300 milliards $" (espace comme séparateur de milliers, symbole $ à la fin).
- N'utilise JAMAIS d'abréviation type "13 $M", ni les mots "trillion"/"trilliard" :
  exprime toujours en millions ou en milliards (1 300 milliards $, pas 1,3 trilliard).
- ATTENTION AU FAUX-AMI : un "billion" anglais vaut 1 000 milliards en français.
  Traduis "4 trillion $" par "4 000 milliards $", jamais par "4 billions $".

EMOJI D'OUVERTURE :
- Commence par UN SEUL emoji thématique, pertinent et sobre. Jamais deux emojis
  côte à côte (pas de "📈 🇺🇸"). Évite les emojis exotiques et la répétition.
  N'emploie "🚀" qu'avec beaucoup de modération.
- Le premier mot après l'emoji prend toujours une MAJUSCULE.
- Grille indicative selon le sujet :
  🚨 news importante / breaking      🔴 alerte ou marché baissier
  📊 données      📈 ou 🟢 marché haussier      📉 ou 🩸 marché baissier
  💬 ou 🎙️ citation      🏦 banque, institution, finance      🇺🇸 (drapeaux) pays
  💵 ou 💰 ou 💸 argent, dollar, stablecoins      📆 date historique      🔐 sécurité
  ⛓️ ou 🔗 blockchain      👮 ou 🕵️ ou 🚔 enquête, arrestation      🤔 news qui questionne
  👀 insolite, intrigant      👨‍⚖️ ou ⚖️ justice      🖼️ ou 🙈 NFT      🗞️ ou 📰 actualité      ⛏️ minage
  ❌ non-événement, absence d'action, démenti (ex. "n'a acheté aucun Bitcoin")

SOURCE :
- Cite une source UNIQUEMENT si c'est une source d'autorité (ex. Bloomberg, Reuters,
  Département de la Justice américain, SEC) apportant une vraie valeur. Dans ce cas
  seulement, termine par "selon [Nom de la source]". Sinon, aucune source.

INTERDIT :
- Pas de hashtags, pas de question rhétorique, pas d'appel à l'engagement.
- Pas de parenthèses, pas de deux-points, pas de tiret long.
"""

# --- EXEMPLES : le modèle imite ces modèles. Ajoutes-en quand un rendu te déplaît. ---
EXEMPLES = """
Voici des exemples de rendu attendu. Imite exactement ce style.

FAITS : $100,000,000 worth of crypto shorts liquidated in the past 60 minutes.
POST : 📉 100 millions $ de positions shorts sur le marché crypto ont été liquidées dans les 60 dernières minutes.

FAITS : President Trump is urging the U.S. Senate to pass the CLARITY Act, warning that China could otherwise take the lead in digital finance and AI.
POST : 🚨 Donald Trump demande au Sénat américain d'adopter le Clarity Act, alertant sur le fait que la Chine pourrait dominer la finance numérique et l'IA.
"""

# --- CORRECTIONS : mauvais rendus déjà observés et leur version corrigée.
#     Ajoute une paire ici chaque fois qu'un rendu te déplaît. ---
CORRECTIONS = """
Voici des rendus qui ont été REFUSÉS et leur version CORRIGÉE. Ne reproduis
jamais les erreurs de la colonne refusée.

REFUSÉ  : 📉 le S&P 500 efface tous ses gains et devient négatif.
CORRIGÉ : 📉 Le S&P 500 efface tous ses gains du jour et passe dans le négatif.

REFUSÉ  : 🏦 25 millions $ de rachat d'actions STRC a été déclaré par Strategy.
CORRIGÉ : 🏦 Strategy annonce avoir effectué un rachat de 25 millions $ de ses actions préférentielles STRC.

REFUSÉ  : 📈 🇺🇸 33,79 millions $ d'encours nets ont afflué dans les Bitcoin Spot ETFs la semaine dernière.
CORRIGÉ : 📈 Les ETF Bitcoin spot américains ont enregistré 33,79 millions $ d'entrées nettes la semaine dernière.

REFUSÉ  : 🚨 BitMart ne proposera plus de services de trading d'ici quelques heures.
CORRIGÉ : 🚨 L'exchange BitMart ne proposera plus de services de trading d'ici quelques heures.

REFUSÉ  : 🔥 Brent Crude a gagné 63 % en six mois.
CORRIGÉ : 🔥 Le cours du pétrole Brent a augmenté de 63 % en six mois.

REFUSÉ  : 🟢 Lido a lancé la migration de plus de 8 millions ETH vers son module Curated v2.
CORRIGÉ : 🟢 Lido lance la migration de plus de 8 millions d'ETH vers son module Curated v2.

REFUSÉ  : 📉 SpaceX $SPCX atteint un nouveau plus bas historique à 110 $.
CORRIGÉ : 📉 L'action SpaceX atteint un nouveau plus bas historique à 110 $.

REFUSÉ  : 📈 Google $GOOGL dépasse à nouveau les 4 billions $ de capitalisation boursière.
CORRIGÉ : 📈 L'action Google dépasse à nouveau les 4 000 milliards $ de capitalisation boursière.

REFUSÉ  : 📊 5,43 millions d'actions MSTR ont été vendues pour 544,5 millions $ entre le 20 et le 26 juillet.
CORRIGÉ : 🚨 Strategy a vendu l'équivalent de 5,43 millions d'actions MSTR pour 544,5 millions $ la semaine dernière.

REFUSÉ  : 📊 Michael Saylor's Strategy n'a acheté aucun Bitcoin au cours du dernier mois.
CORRIGÉ : ❌ Strategy n'a acheté aucun bitcoin au cours du dernier mois.

REFUSÉ  : 🚨 La Federal Reserve n'est pas prévue pour réduire les taux d'intérêt lors de la réunion FOMC de cette semaine.
CORRIGÉ : 🚨 La Réserve fédérale américaine ne devrait pas réduire les taux d'intérêt lors de la réunion FOMC de cette semaine.

REFUSÉ  : 🟢 Des médiateurs progressent pour relancer les pourparlers US-Iran et restaurer un cesse-feu intérimaire.
CORRIGÉ : 🟢 Des médiateurs progressent pour relancer les pourparlers entre les États-Unis et l'Iran et restaurer un cessez-le-feu.

REFUSÉ  : 🚀 Strive annonce l'achat de 79 Bitcoin pour 5,2 millions $.
CORRIGÉ : 🚀 Strive annonce l'achat de 79 bitcoins pour 5,2 millions $.

REFUSÉ  : 🚨 Donald Trump affirme que le taux d'intérêt devrait être abaissé.
CORRIGÉ : 🚨 Donald Trump appelle à une baisse des taux d'intérêt, réitérant sa pression sur la Réserve fédérale pour assouplir sa politique monétaire.
"""

client_groq = Groq(api_key=GROQ_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def maintenant():
    return datetime.now(timezone.utc).isoformat()


def majuscule_initiale(texte):
    """Met une majuscule au premier mot, même s'il est précédé d'un emoji.
    Correction déterministe : garantie à 100 %, sans dépendre du modèle."""
    for i, caractere in enumerate(texte):
        if caractere.isalpha():
            return texte[:i] + caractere.upper() + texte[i + 1:]
    return texte


def rediger(faits):
    """Demande à Groq de rédiger un post Telegram selon la charte."""
    prompt = (
        f"{CHARTE_EDITORIALE}\n"
        f"{EXEMPLES}\n"
        f"{CORRECTIONS}\n"
        "RÈGLE ABSOLUE : utilise UNIQUEMENT les faits ci-dessous. N'invente "
        "aucun chiffre, aucune citation, aucune date, aucun détail. Si une "
        "information manque, ne la mentionne pas.\n\n"
        "Voici les faits :\n\n"
        f"{faits}\n\n"
        "Réponds uniquement par le texte final du post, sans raisonnement, "
        "sans préambule ni guillemets."
    )
    reponse = client_groq.chat.completions.create(
        model=MODELE_GROQ,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1024,
        extra_body={"reasoning_effort": "low"},  # peu de "réflexion" -> il reste du budget pour la réponse
    )
    return majuscule_initiale(reponse.choices[0].message.content.strip())


def main():
    # 1. Clusters ayant déjà un brouillon Telegram (à ne pas refaire)
    drafts_existants = (
        supabase.table("drafts").select("cluster_id").eq("reseau", "telegram").execute().data
    )
    deja_fait = {d["cluster_id"] for d in drafts_existants}

    # 2. Clusters récents, on garde ceux sans brouillon
    limite = (datetime.now(timezone.utc) - timedelta(hours=AGE_MAX_HEURES)).isoformat()
    clusters = (
        supabase.table("clusters")
        .select("id, titre")
        .eq("statut", "actif")
        .gt("activite_le", limite)
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

        # Sécurité : ne jamais enregistrer un brouillon vide (repris au prochain passage)
        if not texte.strip():
            print(f"  Draft vide pour {cid}, on réessaiera au prochain passage.")
            continue

        # 5. Enregistrer le brouillon
        supabase.table("drafts").insert(
            {"cluster_id": cid, "reseau": "telegram", "contenu": texte, "statut": "en_attente"}
        ).execute()
        ecrits += 1

    print(f"Terminé. {ecrits} drafts rédigés.")


if __name__ == "__main__":
    main()
