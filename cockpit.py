"""
Cockpit Telegram — ta console de gestion des news.

À chaque passage :
  1. envoie les brouillons en attente sous forme de cartes enrichies
     (catégorie, ancienneté, sources, liens d'origine) ;
  2. lit tes clics et tes réponses, puis agit :
       ✅ Publier     -> publie sur ta chaîne
       🔄 Reformuler  -> l'IA propose une autre version (seul bouton payant)
       ❌ Rejeter     -> écarte le brouillon
       ⏸️ Différer    -> le représente plus tard
       Pour corriger un texte : réponds simplement à la carte avec ta version.

Les cartes sont envoyées de la plus récente à la plus ancienne.
Tout est déterministe sauf "Reformuler", qui appelle le modèle à la demande.
"""

import os
import requests
from datetime import datetime, timedelta, timezone

from supabase import create_client

# On réutilise la fonction de rédaction (et donc TA charte) de redige.py
from redige import rediger

# --- Secrets (fournis par GitHub) ---
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]        # toi : où arrivent les cartes
CHANNEL = os.environ["TELEGRAM_CHANNEL"]        # ta chaîne : où sont publiés les posts
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Réglages ajustables ---
CARTES_PAR_PASSAGE = 5        # nb de cartes envoyées par passage
HEURES_AVANT_RAPPEL = 6       # un brouillon différé revient après ce délai
AGE_MAX_HEURES = 24           # au-delà, un brouillon non traité est périmé

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def telegram(methode, **params):
    """Appelle l'API Telegram et renvoie la réponse."""
    r = requests.post(f"{API}/{methode}", json=params, timeout=30)
    return r.json()


def maintenant():
    return datetime.now(timezone.utc)


def anciennete(date_iso):
    """Transforme une date en 'il y a 4 min' / 'il y a 3 h'."""
    if not date_iso:
        return ""
    quand = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
    minutes = int((maintenant() - quand).total_seconds() // 60)
    if minutes < 60:
        return f"il y a {max(minutes, 0)} min"
    if minutes < 1440:
        return f"il y a {minutes // 60} h"
    return f"il y a {minutes // 1440} j"


def clavier(draft_id):
    """Les boutons affichés sous chaque carte."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Publier", "callback_data": f"ok:{draft_id}"},
                {"text": "🔄 Reformuler", "callback_data": f"redo:{draft_id}"},
            ],
            [
                {"text": "❌ Rejeter", "callback_data": f"no:{draft_id}"},
                {"text": "⏸️ Différer", "callback_data": f"wait:{draft_id}"},
            ],
        ]
    }


def editer_carte(chat, message_id, texte, boutons=None):
    """Modifie une carte existante et signale les échecs (au lieu de les taire)."""
    params = {
        "chat_id": chat,
        "message_id": message_id,
        "text": texte,
        "disable_web_page_preview": True,
    }
    if boutons:
        params["reply_markup"] = boutons
    rep = telegram("editMessageText", **params)
    if not rep.get("ok"):
        print(f"  Mise à jour de la carte impossible : {rep}")
    return rep


def contexte_cluster(cluster_id):
    """Rassemble tout ce qui aide à décider : contexte, sources, liens."""
    infos = {"entete": "", "sources": "", "liens": [], "faits": "", "nb_sources": 0}

    cl = (
        supabase.table("clusters")
        .select("categorie, premier_vu_le")
        .eq("id", cluster_id)
        .execute()
        .data
    )
    msgs = (
        supabase.table("messages")
        .select("contenu, source_id, externe_id, poste_le")
        .eq("cluster_id", cluster_id)
        .limit(10)
        .execute()
        .data
    )
    srcs = supabase.table("sources").select("id, identifiant").execute().data
    noms = {s["id"]: s["identifiant"] for s in srcs}

    # En-tête : catégorie · ancienneté
    morceaux = []
    if cl:
        if cl[0].get("categorie"):
            morceaux.append(cl[0]["categorie"])
        morceaux.append(anciennete(cl[0].get("premier_vu_le")))
    infos["entete"] = " · ".join(m for m in morceaux if m)

    # Sources distinctes + liens vers les messages d'origine
    vues, liens = [], []
    for m in msgs:
        nom = noms.get(m.get("source_id"))
        if nom and nom not in vues:
            vues.append(nom)
        if nom and m.get("externe_id"):
            liens.append(f"https://t.me/{nom.lstrip('@')}/{m['externe_id']}")
    infos["nb_sources"] = len(vues)
    if vues:
        infos["sources"] = f"📎 {len(vues)} source(s) : " + ", ".join(vues)
    infos["liens"] = liens[:3]

    infos["faits"] = "\n\n".join((m["contenu"] or "")[:500] for m in msgs[:5] if m["contenu"])
    return infos


def construire_carte(draft, entete_resultat=""):
    """Assemble le texte complet de la carte."""
    ctx = contexte_cluster(draft["cluster_id"])
    lignes = []
    if entete_resultat:
        lignes.append(entete_resultat)
    if ctx["entete"]:
        lignes.append(f"📊 {ctx['entete']}")
    lignes.append("")
    lignes.append(draft["contenu"])
    lignes.append("")
    if ctx["sources"]:
        lignes.append(ctx["sources"])
    for lien in ctx["liens"]:
        lignes.append(f"🔗 {lien}")
    if not entete_resultat:
        lignes.append("")
        lignes.append("— Réponds à ce message avec ta version pour corriger le texte.")
    return "\n".join(lignes)


def lire_etat(cle, defaut=""):
    res = supabase.table("etat").select("valeur").eq("cle", cle).execute().data
    return res[0]["valeur"] if res else defaut


def ecrire_etat(cle, valeur):
    supabase.table("etat").upsert({"cle": cle, "valeur": str(valeur)}).execute()


def en_pause():
    return lire_etat("pause", "0") == "1"


def compter(table, filtres=None, depuis=None, colonne_date="maj_le"):
    """Compte les lignes d'une table selon des filtres simples."""
    q = supabase.table(table).select("id", count="exact")
    for col, val in (filtres or {}).items():
        q = q.eq(col, val)
    if depuis:
        q = q.gte(colonne_date, depuis)
    return q.limit(1).execute().count or 0


def lire_offset():
    res = supabase.table("etat").select("valeur").eq("cle", "offset").execute().data
    return int(res[0]["valeur"]) if res else 0


def ecrire_offset(valeur):
    supabase.table("etat").upsert({"cle": "offset", "valeur": str(valeur)}).execute()


# ---------------------------------------------------------------- 1. ENVOI

def envoyer_cartes():
    """Envoie les brouillons en attente, les plus récents d'abord."""
    if en_pause():
        return 0
    limite = (maintenant() - timedelta(hours=HEURES_AVANT_RAPPEL)).isoformat()
    supabase.table("drafts").update(
        {"statut": "en_attente", "message_id": None}
    ).eq("statut", "differe").lt("maj_le", limite).execute()

    perime = (maintenant() - timedelta(hours=AGE_MAX_HEURES)).isoformat()
    supabase.table("drafts").update({"statut": "perime"}).eq(
        "statut", "en_attente"
    ).filter("message_id", "is", "null").lt("cree_le", perime).execute()

    candidats = (
        supabase.table("drafts")
        .select("id, contenu, cluster_id")
        .eq("statut", "en_attente")
        .filter("message_id", "is", "null")
        .order("cree_le", desc=True)
        .limit(30)
        .execute()
        .data
    )
    if not candidats:
        return 0

    envoyees = 0
    for d in candidats[:CARTES_PAR_PASSAGE]:
        rep = telegram(
            "sendMessage",
            chat_id=CHAT_ID,
            text=construire_carte(d),
            reply_markup=clavier(d["id"]),
            disable_web_page_preview=True,
        )
        if rep.get("ok"):
            supabase.table("drafts").update({
                "message_id": rep["result"]["message_id"],
                "nb_sources": contexte_cluster(d["cluster_id"])["nb_sources"],
            }).eq("id", d["id"]).execute()
            envoyees += 1
        else:
            print(f"  Envoi impossible : {rep}")
    return envoyees


def rafraichir_cartes():
    """Met à jour les cartes déjà envoyées quand une source s'ajoute au dossier.

    Le dossier est "vivant" : si une nouvelle chaîne relaie la même actualité,
    la carte se met à jour toute seule. On ne touche PAS au texte du post,
    pour ne pas écraser tes corrections : réponds à la carte pour cela.
    """
    envoyes = (
        supabase.table("drafts")
        .select("id, contenu, cluster_id, message_id, nb_sources")
        .eq("statut", "en_attente")
        .filter("message_id", "not.is", "null")
        .limit(30)
        .execute()
        .data
    )

    majs = 0
    for d in envoyes:
        actuel = contexte_cluster(d["cluster_id"])["nb_sources"]
        if actuel == (d.get("nb_sources") or 0):
            continue  # rien de neuf : on ne modifie pas le message
        rep = editer_carte(CHAT_ID, d["message_id"], construire_carte(d), clavier(d["id"]))
        if rep.get("ok"):
            supabase.table("drafts").update(
                {"nb_sources": actuel}
            ).eq("id", d["id"]).execute()
            majs += 1
    return majs


# ------------------------------------------------------- 2. LECTURE DES CLICS

def publier(draft):
    """Publie le brouillon sur la chaîne, puis enregistre la publication."""
    rep = telegram("sendMessage", chat_id=CHANNEL, text=draft["contenu"])
    if not rep.get("ok"):
        return False, rep
    supabase.table("drafts").update(
        {"statut": "publie", "maj_le": maintenant().isoformat()}
    ).eq("id", draft["id"]).execute()
    supabase.table("publications").insert({
        "draft_id": draft["id"],
        "reseau": "telegram",
        "post_externe_id": str(rep["result"]["message_id"]),
    }).execute()
    return True, rep


def traiter_clic(cb):
    """Traite un appui sur un bouton."""
    action, draft_id = cb["data"].split(":", 1)
    message_id = cb["message"]["message_id"]
    chat = cb["message"]["chat"]["id"]

    res = (
        supabase.table("drafts")
        .select("id, contenu, statut, cluster_id")
        .eq("id", draft_id)
        .execute()
        .data
    )
    if not res:
        telegram("answerCallbackQuery", callback_query_id=cb["id"], text="Brouillon introuvable.")
        return
    draft = res[0]

    if draft["statut"] != "en_attente":
        telegram("answerCallbackQuery", callback_query_id=cb["id"], text="Déjà traité.")
        return

    # --- Reformuler : l'IA propose une nouvelle version à partir des mêmes faits ---
    if action == "redo":
        ctx = contexte_cluster(draft["cluster_id"])
        if not ctx["faits"].strip():
            telegram("answerCallbackQuery", callback_query_id=cb["id"],
                     text="Aucun fait source disponible.", show_alert=True)
            return
        try:
            propose = rediger(ctx["faits"])
        except Exception as e:
            # On affiche l'erreur directement dans Telegram, pas seulement dans les logs
            telegram("answerCallbackQuery", callback_query_id=cb["id"],
                     text=f"Reformulation impossible : {str(e)[:150]}", show_alert=True)
            print(f"  Reformulation échouée : {e}")
            return
        if not propose.strip():
            telegram("answerCallbackQuery", callback_query_id=cb["id"],
                     text="Le modèle n'a rien renvoyé, réessaie.", show_alert=True)
            return

        supabase.table("drafts").update(
            {"contenu": propose, "maj_le": maintenant().isoformat()}
        ).eq("id", draft_id).execute()
        draft["contenu"] = propose
        editer_carte(chat, message_id,
                     construire_carte(draft, entete_resultat="🔄 Reformulé par l'IA"),
                     clavier(draft_id))
        telegram("answerCallbackQuery", callback_query_id=cb["id"], text="Nouvelle version.")
        return

    # --- Les trois actions qui closent la carte ---
    heure = maintenant().strftime("%H:%M UTC")

    if action == "ok":
        ok, rep = publier(draft)
        entete = f"✅ PUBLIÉ à {heure}" if ok else f"⚠️ ÉCHEC : {rep.get('description', '')}"
        note = "Publié sur la chaîne." if ok else "Publication impossible."
    elif action == "no":
        supabase.table("drafts").update(
            {"statut": "rejete", "maj_le": maintenant().isoformat()}
        ).eq("id", draft_id).execute()
        entete, note = f"❌ REJETÉ à {heure}", "Écarté."
    else:  # wait
        supabase.table("drafts").update(
            {"statut": "differe", "maj_le": maintenant().isoformat()}
        ).eq("id", draft_id).execute()
        entete = f"⏸️ DIFFÉRÉ à {heure}"
        note = f"Reviendra dans {HEURES_AVANT_RAPPEL} h."

    # La carte porte désormais le résultat de ton action, et perd ses boutons
    editer_carte(chat, message_id, construire_carte(draft, entete_resultat=entete))
    telegram("answerCallbackQuery", callback_query_id=cb["id"], text=note)


def traiter_reponse(msg):
    """Traite une réponse à une carte : remplace le texte du brouillon."""
    original = msg["reply_to_message"]["message_id"]
    nouveau_texte = (msg.get("text") or "").strip()
    if not nouveau_texte:
        return

    # Ta réponse peut viser la carte elle-même, ou la demande de texte
    champs = "id, cluster_id, message_id"
    res = (
        supabase.table("drafts").select(champs)
        .eq("message_id", original).eq("statut", "en_attente").execute().data
    )
    if not res:
        res = (
            supabase.table("drafts").select(champs)
            .eq("prompt_message_id", original).eq("statut", "en_attente").execute().data
        )
    if not res:
        return

    draft = {
        "id": res[0]["id"],
        "cluster_id": res[0]["cluster_id"],
        "contenu": nouveau_texte,
    }
    carte_id = res[0]["message_id"]

    supabase.table("drafts").update(
        {"contenu": nouveau_texte, "maj_le": maintenant().isoformat()}
    ).eq("id", draft["id"]).execute()

    # On met à jour la carte (et non le message de demande)
    editer_carte(msg["chat"]["id"], carte_id,
                 construire_carte(draft, entete_resultat="✏️ Modifié"),
                 clavier(draft["id"]))

    # On efface la demande de texte pour garder la conversation propre
    if original != carte_id:
        telegram("deleteMessage", chat_id=msg["chat"]["id"], message_id=original)


AIDE = (
    "🎛️ Commandes disponibles\n\n"
    "/etat — file d'attente et activité du jour\n"
    "/pause — arrête l'envoi de nouvelles cartes\n"
    "/reprendre — relance l'envoi\n"
    "/sources — liste tes sources\n"
    "/ajouter @chaine — ajoute une source Telegram\n"
    "/retirer @chaine — désactive une source\n"
    "/aide — affiche ce message"
)


def traiter_commande(msg):
    """Exécute une commande envoyée dans la conversation avec le bot."""
    chat = msg["chat"]["id"]
    if str(chat) != str(CHAT_ID):
        return  # le bot n'obéit qu'à toi

    texte = (msg.get("text") or "").strip()
    commande, _, argument = texte.partition(" ")
    commande = commande.lower().split("@")[0]   # /etat@MonBot -> /etat
    argument = argument.strip()

    if commande in ("/aide", "/start", "/help"):
        rep = telegram("sendMessage", chat_id=chat, text=AIDE)
        if rep.get("ok"):
            # On retire l'ancienne épingle du bot, puis on épingle la nouvelle aide
            ancienne = lire_etat("aide_epinglee")
            if ancienne:
                telegram("unpinChatMessage", chat_id=chat, message_id=int(ancienne))
            nouvelle = rep["result"]["message_id"]
            epingle = telegram("pinChatMessage", chat_id=chat, message_id=nouvelle,
                               disable_notification=True)
            if epingle.get("ok"):
                ecrire_etat("aide_epinglee", nouvelle)
                print("  Aide épinglée.")
            else:
                # On le signale au lieu de le taire
                print(f"  Épinglage impossible : {epingle}")
                telegram("sendMessage", chat_id=chat,
                         text="⚠️ Je n'ai pas pu épingler ce message. "
                              "Tu peux l'épingler à la main : appui long → Épingler.")

    elif commande == "/etat":
        debut_jour = maintenant().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        lignes = [
            "📊 État du système\n",
            f"En attente de validation : {compter('drafts', {'statut': 'en_attente'})}",
            f"Différés : {compter('drafts', {'statut': 'differe'})}",
            f"Publiés aujourd'hui : {compter('drafts', {'statut': 'publie'}, debut_jour)}",
            f"Rejetés aujourd'hui : {compter('drafts', {'statut': 'rejete'}, debut_jour)}",
            "",
            f"Messages en attente de tri : {compter('messages', {'cluster_id': None})}",
            f"Sources actives : {compter('sources', {'actif': True})}",
            "",
            "⏸️ Envoi en pause" if en_pause() else "▶️ Envoi actif",
        ]
        telegram("sendMessage", chat_id=chat, text="\n".join(lignes))

    elif commande == "/pause":
        ecrire_etat("pause", "1")
        telegram("sendMessage", chat_id=chat,
                 text="⏸️ Envoi mis en pause. La collecte continue en arrière-plan.\n"
                      "Utilise /reprendre pour relancer.")

    elif commande == "/reprendre":
        ecrire_etat("pause", "0")
        telegram("sendMessage", chat_id=chat, text="▶️ Envoi relancé.")

    elif commande == "/sources":
        srcs = (
            supabase.table("sources").select("identifiant, actif, type")
            .order("identifiant").execute().data
        )
        if not srcs:
            telegram("sendMessage", chat_id=chat, text="Aucune source enregistrée.")
            return
        lignes = ["📡 Tes sources\n"] + [
            f"{'✅' if s['actif'] else '⛔'} {s['identifiant']}" for s in srcs
        ]
        telegram("sendMessage", chat_id=chat, text="\n".join(lignes))

    elif commande == "/ajouter":
        if not argument.startswith("@"):
            telegram("sendMessage", chat_id=chat,
                     text="Usage : /ajouter @nomdelachaine")
            return
        existe = (
            supabase.table("sources").select("id")
            .eq("type", "telegram").eq("identifiant", argument).execute().data
        )
        if existe:
            supabase.table("sources").update({"actif": True}).eq("id", existe[0]["id"]).execute()
            reponse = f"✅ {argument} réactivée."
        else:
            supabase.table("sources").insert(
                {"type": "telegram", "identifiant": argument, "actif": True}
            ).execute()
            reponse = f"✅ {argument} ajoutée."
        telegram("sendMessage", chat_id=chat,
                 text=reponse + "\n\n⚠️ Pense à abonner ton compte userbot à cette chaîne, "
                      "sinon il ne pourra pas la lire.")

    elif commande == "/retirer":
        if not argument.startswith("@"):
            telegram("sendMessage", chat_id=chat, text="Usage : /retirer @nomdelachaine")
            return
        res = (
            supabase.table("sources").update({"actif": False})
            .eq("type", "telegram").eq("identifiant", argument).execute().data
        )
        telegram("sendMessage", chat_id=chat,
                 text=f"⛔ {argument} désactivée." if res else f"{argument} introuvable.")

    else:
        telegram("sendMessage", chat_id=chat,
                 text="Commande inconnue. Tape /aide pour la liste.")


def lire_actions():
    """Récupère les clics et réponses depuis le dernier passage."""
    offset = lire_offset()
    rep = telegram("getUpdates", offset=offset, timeout=0, limit=50)
    if not rep.get("ok"):
        print(f"  Lecture impossible : {rep}")
        return 0

    updates = rep["result"]
    traites = 0
    for u in updates:
        try:
            if "callback_query" in u:
                traiter_clic(u["callback_query"])
                traites += 1
            elif "message" in u and "reply_to_message" in u["message"]:
                traiter_reponse(u["message"])
                traites += 1
            elif "message" in u and (u["message"].get("text") or "").startswith("/"):
                traiter_commande(u["message"])
                traites += 1
        except Exception as e:
            print(f"  Action ignorée : {e}")

    if updates:
        ecrire_offset(updates[-1]["update_id"] + 1)
    return traites


def main():
    traites = lire_actions()       # d'abord tes actions
    majs = rafraichir_cartes()     # puis les dossiers qui se sont enrichis
    envoyees = envoyer_cartes()    # enfin les nouvelles cartes
    print(f"Terminé. {traites} actions traitées, {majs} cartes mises à jour, "
          f"{envoyees} cartes envoyées.")


if __name__ == "__main__":
    main()
