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

import requests
from supabase import create_client

# --- Secrets (fournis par GitHub) ---
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")   # optionnel
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")         # utilisé si Mistral absent
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Réglages ajustables ---
# Le fournisseur est choisi tout seul : Mistral si sa clé existe, sinon Groq.
# Pour basculer sur Mistral, il suffit d'ajouter le secret MISTRAL_API_KEY.
if MISTRAL_API_KEY:
    URL_API = "https://api.mistral.ai/v1/chat/completions"
    CLE_API = MISTRAL_API_KEY
    MODELE = "mistral-large-latest"     # modèle français, meilleur en reformulation
else:
    URL_API = "https://api.groq.com/openai/v1/chat/completions"
    CLE_API = GROQ_API_KEY
    MODELE = "openai/gpt-oss-120b"
CLUSTERS_PAR_PASSAGE = 10                   # nombre de drafts rédigés par passage
MESSAGES_PAR_CLUSTER = 5                    # faits max transmis au LLM
AGE_MAX_HEURES = 6                          # on ne rédige que pour l'actu fraîche

# --- CHARTE ÉDITORIALE : c'est ici que vit TON style. Modifie librement. ---
CHARTE_EDITORIALE = """
Tu écris un post pour la chaîne Telegram d'un média crypto francophone.

OBJECTIF : transmettre l'information principale de façon complète, mais la plus
claire et concise possible.

PRINCIPE CARDINAL — REFORMULE, NE TRADUIS PAS :
Les faits te sont fournis en anglais. Ne les traduis JAMAIS mot à mot.
Lis-les, comprends l'information, puis RÉÉCRIS-LA de zéro comme un journaliste
français l'écrirait spontanément. Le résultat doit se lire comme un texte pensé
en français, jamais comme une traduction.
Si une tournure te semble calquée sur l'anglais, réécris-la autrement.
Aucun mot ni segment ne doit rester en anglais, hormis les noms propres et les
anglicismes courants du vocabulaire crypto.

LONGUEUR ET STRUCTURE :
- Produis UN SEUL post, d'un seul tenant. N'écris JAMAIS deux blocs à la suite,
  et n'emploie qu'UN SEUL emoji dans tout le message, en première position.
- Si les faits couvrent plusieurs aspects d'un même événement, fais-en UNE
  SYNTHÈSE dans une phrase unique. Ne les juxtapose pas.
- ÉLAGUE les détails secondaires. Garde ce qui compte, supprime le reste
  (sous-totaux par fonds, noms de filiales, précisions techniques accessoires).
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
- Mets l'article devant les noms de pays : "L'Iran rejette", jamais "Iran rejette".
  Quand l'information concerne un pays précis, son drapeau est un bon emoji.
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
- ATTENTION AU FAUX-AMI : "trillion" et "billion" anglais valent 1 000 milliards.
  Écris "4 000 milliards $" pour "4 trillion $", et "1 800 milliards $" pour
  "1,8 trillion $". N'emploie jamais les mots "trillion" ni "billion" en français.
- Si un chiffre ne s'articule pas naturellement dans la phrase, OMETS-LE plutôt
  que de le recoller de force. Mieux vaut un post juste sans le chiffre qu'une
  phrase absurde. N'écris jamais "soutenir le Clarity Act pour 1 700 milliards $
  d'actifs" : ce chiffre qualifie la société, pas son soutien.

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

REFUSÉ  : 🟢 La Hongrie abroge l'obligation de validation tierce pour certaines conversions crypto, supprimant les exigences de vérification d'origine des actifs. 🟢 La Banque nationale de Hongrie a accordé à CoinCash la première licence MiCA du pays le 20 juillet, couvrant la garde, le crypto-to-fiat et le crypto-to-crypto.
CORRIGÉ : 🇭🇺 Le Parlement hongrois vote l'abrogation de son régime de validation des transactions crypto, qui imposait de certifier chaque conversion sous peine de prison et avait fait fuir de nombreux acteurs. En parallèle, CoinCash devient le premier opérateur hongrois agréé sous MiCA.

REFUSÉ  : 📈 Les prix du pétrole ont augmenté de 5 % après que l'Iran a lancé une attaque surprise contre une base américaine en Jordanie.
CORRIGÉ : 📈 Le cours du pétrole Brent augmente de 5 % après que l'Iran a lancé une attaque surprise contre une base américaine en Jordanie.

REFUSÉ  : 🚨 Iran rejette la proposition d'Oman pour la gestion conjointe du détroit d'Ormuz.
CORRIGÉ : 🚨 L'Iran rejette la proposition d'Oman pour la gestion conjointe du détroit d'Ormuz.

REFUSÉ  : 📉 Les ETF Bitcoin spot américains ont enregistré 11,64 millions $ de sorties nettes le 27 juillet, le fonds IBIT de BlackRock affichant la plus forte sortie avec 8,82 millions $. 📈 Les ETF Ethereum spot ont enregistré 9,23 millions $ d'entrées nettes, BlackRock ETHA menant avec 11,75 millions $.
CORRIGÉ : 📉 Les ETF Bitcoin spot américains ont enregistré hier 11,64 millions $ de sorties nettes, tandis que les ETF Ethereum spot ont enregistré 9,23 millions $ d'entrées nettes.

REFUSÉ  : 🇺🇸 America's biggest companies annoncent reprendre les recrutements alors que la demande de main-d'œuvre augmente avec l'IA.
CORRIGÉ : 🇺🇸 Les plus grandes sociétés américaines annoncent reprendre les recrutements alors que la demande de main-d'œuvre augmente avec l'IA.

REFUSÉ  : 🏦 Franklin Templeton annonce soutenir le Clarity Act pour 1 700 milliards $ d'actifs.
CORRIGÉ : 🏦 Le gestionnaire d'actifs Franklin Templeton annonce soutenir le Clarity Act.

REFUSÉ  : 🚨 Franklin Templeton, gestionnaire d'actifs de 1,8 trillion $, demande au Sénat américain d'adopter le Clarity Act.
CORRIGÉ : 🚨 Franklin Templeton, gestionnaire d'actifs de 1 800 milliards $, demande au Sénat américain d'adopter le Clarity Act.

REFUSÉ  : 📊 Michael Saylor's Strategy n'a acheté aucun Bitcoin au cours du dernier mois.
CORRIGÉ : ❌ Strategy n'a acheté aucun Bitcoin au cours du dernier mois.

REFUSÉ  : 🚨 La Federal Reserve n'est pas prévue pour réduire les taux d'intérêt lors de la réunion FOMC de cette semaine.
CORRIGÉ : 🚨 La Réserve fédérale américaine ne devrait pas réduire les taux d'intérêt lors de la réunion FOMC de cette semaine.

REFUSÉ  : 🟢 Des médiateurs progressent pour relancer les pourparlers US-Iran et restaurer un cesse-feu intérimaire.
CORRIGÉ : 🟢 Des médiateurs progressent pour relancer les pourparlers entre les États-Unis et l'Iran et restaurer un cessez-le-feu.
"""

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
        "information manque, ne la mentionne pas.\n"
        "N'ATTRIBUE JAMAIS un fait à une entreprise, une personne ou un pays "
        "autre que celui explicitement nommé dans les faits. Ne remplace "
        "jamais un nom peu connu par un nom plus connu.\n"
        "Si les faits ci-dessous mêlent visiblement DEUX événements sans "
        "rapport, ne traite QUE le premier.\n\n"
        "Voici les faits :\n\n"
        f"{faits}\n\n"
        "Réponds uniquement par le texte final du post, sans raisonnement, "
        "sans préambule ni guillemets."
    )
    corps = {
        "model": MODELE,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 400,
    }
    if not MISTRAL_API_KEY:
        # gpt-oss "réfléchit" avant d'écrire : on limite pour garder du budget
        corps["reasoning_effort"] = "low"
        corps["max_tokens"] = 1024

    reponse = requests.post(
        URL_API,
        headers={
            "Authorization": f"Bearer {CLE_API}",
            "Content-Type": "application/json",
        },
        json=corps,
        timeout=60,
    )
    reponse.raise_for_status()
    texte = reponse.json()["choices"][0]["message"]["content"].strip()
    return majuscule_initiale(texte)


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
            .select("contenu, poste_le")
            .eq("cluster_id", cid)
            .limit(MESSAGES_PAR_CLUSTER)
            .execute()
            .data
        )
        faits = "\n\n".join((m["contenu"] or "")[:500] for m in msgs if m["contenu"])
        if not faits.strip():
            continue

        # Garde-fou : on se base sur la VRAIE date de l'actualité, pas sur la
        # date de création du dossier. Une vieille news ne produit pas de post.
        dates = [m["poste_le"] for m in msgs if m.get("poste_le")]
        if dates:
            plus_recente = max(
                datetime.fromisoformat(d.replace("Z", "+00:00")) for d in dates
            )
            heures = (datetime.now(timezone.utc) - plus_recente).total_seconds() / 3600
            if heures > AGE_MAX_HEURES:
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
