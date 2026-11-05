import os
import re
import random
import asyncio
import time
import ssl
import html
import sqlite3
from datetime import datetime, timezone, timedelta
import json
import smtplib
from email.message import EmailMessage
import httpx
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, WebAppInfo,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

NOWPAY_KEY = os.environ["NOWPAY_KEY"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
API = "https://api.nowpayments.io/v1"
CODES = {"USDT TRC20": "usdttrc20", "USDT BEP20": "usdtbsc"}

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", SMTP_USER)
MINIAPP_URL = os.environ.get("MINIAPP_URL", "")
MAXIMUM = 100000
REGEX_EMAIL = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"

TAUX_FCFA = 500
FRAIS_SERVICE = 0.03
FRAIS_RESEAU = {"USDT TRC20": 1.0, "USDT BEP20": 0.5}
MINIMUM = 20
DELAI_SECONDES = 20 * 60
SURVEILLANCE_APRES = 6 * 3600
SEP = "━━━━━━━━━━━━━━━"
TACHES = set()
ETATS_FINAUX = ("paye", "envoye", "cloturee", "termine", "probleme")

ACCUEIL = (
    "👋 <b>Bienvenue sur X_change</b>\n"
    f"{SEP}\n"
    "Vendez votre crypto et recevez du <b>Mobile Money</b> "
    "(MTN, Moov, Celtis).\n\n"
    "• Minimum : <b>20 USDT</b>\n"
    "• Frais : <b>3 %</b>\n"
    "• Délai de paiement : <b>20 minutes</b>"
)

def fcfa_txt(x):
    return f"{x:,.0f}".replace(",", " ")

def calcul(m, crypto):
    frais = m * FRAIS_SERVICE
    reseau = FRAIS_RESEAU[crypto]
    fcfa = (m - frais - reseau) * TAUX_FCFA
    return frais, reseau, fcfa

def _envoyer_email(dest, sujet, corps):
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = dest
    msg["Subject"] = sujet
    msg.set_content(corps)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as srv:
        srv.login(SMTP_USER, SMTP_PASS)
        srv.send_message(msg)

async def envoyer_email(dest, sujet, corps):
    if not (SMTP_USER and SMTP_PASS and dest):
        return
    try:
        await asyncio.to_thread(_envoyer_email, dest, sujet, corps)
    except Exception as e:
        print("Email impossible :", type(e).__name__, e)

def lancer_email(dest, sujet, corps):
    t = asyncio.create_task(envoyer_email(dest, sujet, corps))
    TACHES.add(t)
    t.add_done_callback(TACHES.discard)

def texte_verif(u):
    crypto, m = u["crypto"], u["montant"]
    _, _, fcfa = calcul(m, crypto)
    mail = u.get("email") or "non renseigné"
    return (
        "🔎 <b>Vérifiez votre commande</b>\n"
        f"{SEP}\n"
        f"Vous envoyez : <b>{m:.2f} {crypto}</b>\n"
        f"Vous recevez : <b>{fcfa_txt(fcfa)} FCFA</b>\n"
        f"Réseau : <b>{u['operateur']}</b>\n"
        f"Numéro : <b>{u['numero']}</b>\n"
        f"Titulaire : <b>{u['nom']}</b>\n"
        f"E-mail : <b>{mail}</b>\n\n"
        f"⚠️ Le numéro doit être un compte <b>{u['operateur']}</b> au nom "
        "indiqué. En cas d'erreur, votre paiement sera retardé.\n\n"
        "Les informations sont-elles correctes ?"
    )

# --- HISTORIQUE DEBUT ---
DB = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "xchange.db"))
DB.row_factory = sqlite3.Row
DB.execute("""CREATE TABLE IF NOT EXISTS commandes (
    num TEXT PRIMARY KEY, user_id INTEGER, chat_id INTEGER, message_id INTEGER,
    payment_id TEXT, crypto TEXT, montant REAL, fcfa REAL, operateur TEXT,
    numero TEXT, nom TEXT, email TEXT, etat TEXT, cree_le INTEGER, maj_le INTEGER)""")
DB.execute("CREATE INDEX IF NOT EXISTS idx_user ON commandes(user_id, cree_le)")
DB.commit()
FUSEAU = timezone(timedelta(hours=1))
STATUTS = {
    "attente": "⏳ En attente du paiement",
    "detecte": "🔎 Paiement détecté",
    "paye": "💰 Paiement reçu, envoi en cours",
    "envoye": "💸 Mobile Money envoyé",
    "cloturee": "✅ Terminée",
    "annulee": "❌ Annulée",
    "expiree": "⌛ Expirée",
    "termine": "❌ Non aboutie",
    "probleme": "⚠️ À vérifier (support)",
}

def date_txt(ts):
    return datetime.fromtimestamp(ts, FUSEAU).strftime("%d/%m/%Y à %H:%M")

def date_court(ts):
    return datetime.fromtimestamp(ts, FUSEAU).strftime("%d/%m %H:%M")

def db_creer(num, cmd):
    now = int(time.time())
    cmd["cree_le"] = now
    fcfa = calcul(cmd["montant"], cmd["crypto"])[2]
    try:
        DB.execute(
            "INSERT OR REPLACE INTO commandes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (num, cmd.get("user_id"), cmd["chat_id"], cmd["message_id"],
             str(cmd["payment_id"]), cmd["crypto"], cmd["montant"], fcfa,
             cmd["operateur"], cmd["numero"], cmd["nom"], cmd.get("email"),
             cmd["etat"], now, now),
        )
        DB.commit()
    except Exception as e:
        print("Base de données :", type(e).__name__, e)

def db_maj(num, cmd):
    try:
        DB.execute(
            "UPDATE commandes SET etat=?, fcfa=COALESCE(?, fcfa), maj_le=? WHERE num=?",
            (cmd["etat"], cmd.get("fcfa"), int(time.time()), num),
        )
        DB.commit()
    except Exception as e:
        print("Base de données :", type(e).__name__, e)

def obtenir_cmd(commandes, num):
    if num in commandes:
        return commandes[num]
    r = DB.execute("SELECT * FROM commandes WHERE num=?", (num,)).fetchone()
    if not r:
        return None
    cmd = dict(r)
    cmd["debut"] = r["cree_le"]
    commandes[num] = cmd
    return cmd

def texte_cmd(r):
    t = (
        f"📄 <b>Commande {r['num']}</b>\n"
        f"{SEP}\n"
        f"Statut : <b>{STATUTS.get(r['etat'], r['etat'])}</b>\n"
        f"Créée le : <b>{date_txt(r['cree_le'])}</b>\n"
        f"Dernière mise à jour : {date_txt(r['maj_le'])}\n\n"
        f"Montant : <b>{r['montant']:.2f} {r['crypto']}</b>\n"
        f"Vous recevez : <b>{fcfa_txt(r['fcfa'])} FCFA</b>\n"
        f"Réseau : {r['operateur']}\n"
        f"Numéro : {r['numero']}\n"
        f"Titulaire : {r['nom']}\n"
    )
    if r["email"]:
        t += f"E-mail : {r['email']}\n"
    return t + "\n<i>Heure du Bénin (UTC+1)</i>"

def kb_detail(r):
    lignes = []
    if r["etat"] == "envoye":
        lignes.append([("✅ J’ai reçu mon argent", "recu_" + r["num"])])
    lignes.append([("🆘 Un problème avec cette commande", "probleme_" + r["num"])])
    lignes.append([("⬅️ Mon espace", "espace")])
    return clavier(lignes)

def construire_espace(uid, nom):
    rows = DB.execute(
        "SELECT * FROM commandes WHERE user_id=? ORDER BY cree_le DESC LIMIT 10", (uid,)
    ).fetchall()
    tot = DB.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN etat IN ('envoye','cloturee') "
        "THEN fcfa END), 0) AS s FROM commandes WHERE user_id=?", (uid,)
    ).fetchone()
    texte = (
        f"📂 <b>Mon espace</b>\n"
        f"{SEP}\n"
        f"👤 {html.escape(nom)}\n"
        f"Commandes : <b>{tot['n']}</b>\n"
        f"Total envoyé sur Mobile Money : <b>{fcfa_txt(tot['s'])} FCFA</b>\n\n"
    )
    if rows:
        texte += "Vos 10 dernières commandes. Appuyez sur l’une d’elles pour voir le détail."
    else:
        texte += "Aucune commande pour le moment."
    lignes = [
        [(f"{r['num']} · {date_court(r['cree_le'])} · "
          f"{STATUTS.get(r['etat'], '❔').split()[0]}", "cmd_" + r["num"])]
        for r in rows
    ]
    lignes.append([("🔎 Chercher par code", "chercher")])
    lignes.append([("⬅️ Accueil", "accueil")])
    return texte, clavier(lignes)
# --- HISTORIQUE FIN ---

def clavier(lignes):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in ligne] for ligne in lignes]
    )

def kb_accueil():
    return clavier([
        [("💱 Vendre ma crypto", "vendre")],
        [("📂 Mon espace", "espace")],
        [("🆘 Aide / Support", "aide")],
    ])

def kb_cryptos():
    return clavier([
        [("USDT TRC20", "crypto_usdt_trc20"), ("USDT BEP20", "crypto_usdt_bep20")],
        [("BTC", "crypto_btc"), ("TRX", "crypto_trx")],
        [("⬅️ Retour", "accueil")],
    ])

def kb_annuler():
    return clavier([[("❌ Annuler", "annuler")]])

def kb_recap():
    return clavier([
        [("✅ Continuer", "continuer")],
        [("❌ Annuler", "annuler")],
    ])

def kb_passer_email():
    return clavier([
        [("⏭ Passer", "sans_email")],
        [("❌ Annuler", "annuler")],
    ])

def kb_operateurs():
    return clavier([
        [("MTN", "op_MTN"), ("Moov", "op_Moov"), ("Celtis", "op_Celtis")],
        [("❌ Annuler", "annuler")],
    ])

def kb_confirmation():
    return clavier([
        [("✅ Confirmer", "confirmer")],
        [("✏️ Modifier mes informations", "modifier_num")],
        [("❌ Annuler", "annuler")],
    ])

async def creer_paiement(m, crypto, num):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            API + "/payment",
            headers={"x-api-key": NOWPAY_KEY},
            json={
                "price_amount": m,
                "price_currency": "usd",
                "pay_currency": CODES[crypto],
                "order_id": num,
                "order_description": "X_change " + num,
            },
        )
        r.raise_for_status()
        return r.json()

async def statut_paiement(pid):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            API + f"/payment/{pid}",
            headers={"x-api-key": NOWPAY_KEY},
        )
        r.raise_for_status()
        return r.json()

async def notifier_admin(bot, texte, kb=None):
    print(texte)
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=texte, reply_markup=kb)
    except Exception as e:
        print("Notification admin impossible :", type(e).__name__)

async def modifier(bot, chat_id, message_id, texte, kb=None):
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=texte, parse_mode="HTML", reply_markup=kb,
        )
    except Exception:
        pass

async def finaliser_paiement(bot, num, cmd, recu):
    base = min(recu, cmd["montant"])
    if base < MINIMUM:
        cmd["etat"] = "probleme"
        db_maj(num, cmd)
        await modifier(
            bot, cmd["chat_id"], cmd["message_id"],
            f"⚠️ <b>Commande {num}</b>\n"
            "Le montant reçu est inférieur au minimum de 20 USDT.\n"
            "Contactez le support avec votre numéro de commande.",
            kb_accueil(),
        )
        await notifier_admin(
            bot,
            f"⚠️ {num} : montant reçu {recu} sous le minimum. À traiter manuellement.",
        )
        return
    cmd["etat"] = "paye"
    db_maj(num, cmd)
    _, _, fcfa = calcul(base, cmd["crypto"])
    cmd["fcfa"] = fcfa
    db_maj(num, cmd)
    await modifier(
        bot, cmd["chat_id"], cmd["message_id"],
        f"✅ <b>Paiement confirmé — commande {num}</b>\n"
        f"{SEP}\n"
        f"Reçu : <b>{base:.2f} {cmd['crypto']}</b>\n"
        f"Envoi de <b>{fcfa_txt(fcfa)} FCFA</b> en cours sur "
        f"{cmd['operateur']} au {cmd['numero']}.\n\n"
        "Vous serez crédité dans quelques minutes.",
    )
    await notifier_admin(
        bot,
        f"💰 PAIEMENT REÇU {num}\n"
        f"{base:.2f} {cmd['crypto']}\n"
        f"À envoyer : {fcfa_txt(fcfa)} FCFA\n"
        f"Réseau : {cmd['operateur']}\n"
        f"Numéro : {cmd['numero']}\n"
        f"Titulaire : {cmd['nom']}\n"
        "⚠️ Vérifiez que le nom correspond avant d'envoyer.",
        clavier([[("✅ J'ai envoyé le Mobile Money", "admpaye_" + num)]]),
    )
    lancer_email(
        ADMIN_EMAIL,
        f"[X_change] Paiement reçu {num}",
        f"PAIEMENT REÇU - commande {num}\n\n"
        f"Montant reçu : {base:.2f} {cmd['crypto']}\n"
        f"À envoyer : {fcfa_txt(fcfa)} FCFA\n"
        f"Réseau : {cmd['operateur']}\n"
        f"Numéro : {cmd['numero']}\n"
        f"Titulaire : {cmd['nom']}\n"
        f"E-mail client : {cmd.get('email') or 'non renseigné'}\n\n"
        "Vérifiez que le nom correspond avant d'envoyer le Mobile Money.",
    )

async def surveiller(bot, num, commandes, debut=None):
    cmd = commandes[num]
    debut = debut or time.time()
    while True:
        await asyncio.sleep(15 if cmd["etat"] in ("attente", "detecte") else 60)
        etat = cmd["etat"]
        if etat in ETATS_FINAUX:
            return
        ecoule = time.time() - debut
        if ecoule > DELAI_SECONDES + SURVEILLANCE_APRES:
            return
        if etat == "attente" and ecoule > DELAI_SECONDES:
            cmd["etat"] = "expiree"
            db_maj(num, cmd)
            etat = "expiree"
            await modifier(
                bot, cmd["chat_id"], cmd["message_id"],
                f"⌛ <b>Commande {num} annulée</b>\n"
                "Le délai de 20 minutes est dépassé.\n\n"
                "<b>N'envoyez plus de crypto à cette adresse.</b>\n"
                "Pour recommencer, appuyez sur le bouton ci-dessous.",
                kb_accueil(),
            )
        try:
            info = await statut_paiement(cmd["payment_id"])
        except Exception:
            continue
        st = info.get("payment_status")

        if etat in ("annulee", "expiree"):
            if st in ("failed", "expired", "refunded"):
                return
            if st in ("confirming", "confirmed", "sending", "finished", "partially_paid"):
                cmd["etat"] = "probleme"
                db_maj(num, cmd)
                await notifier_admin(
                    bot,
                    f"🚨 PAIEMENT SUR COMMANDE {etat.upper()} {num}\n"
                    f"{cmd['montant']:.2f} {cmd['crypto']} (montant commandé)\n"
                    f"Réseau : {cmd['operateur']}\n"
                    f"Numéro : {cmd['numero']}\n"
                    f"Titulaire : {cmd['nom']}\n"
                    "À traiter manuellement.",
                )
                try:
                    await bot.send_message(
                        chat_id=cmd["chat_id"],
                        text=(
                            f"⚠️ Nous avons détecté un paiement sur votre commande {num}, "
                            "qui était annulée ou expirée.\n"
                            "Notre support va vous contacter. Vous pouvez aussi nous écrire."
                        ),
                        reply_markup=clavier([[("🆘 Contacter le support", "probleme_" + num)]]),
                    )
                except Exception:
                    pass
                return
            continue

        if st in ("confirming", "confirmed", "sending") and etat == "attente":
            cmd["etat"] = "detecte"
            db_maj(num, cmd)
            await modifier(
                bot, cmd["chat_id"], cmd["message_id"],
                f"🔎 <b>Commande {num}</b>\n"
                "Paiement détecté, en attente de confirmation du réseau…",
            )

        elif st == "finished":
            try:
                recu = float(info.get("actually_paid") or 0)
            except (TypeError, ValueError):
                recu = 0
            if recu <= 0:
                recu = cmd["montant"]
            await finaliser_paiement(bot, num, cmd, recu)
            return

        elif st in ("failed", "expired", "refunded") and etat == "detecte":
            cmd["etat"] = "termine"
            db_maj(num, cmd)
            await modifier(
                bot, cmd["chat_id"], cmd["message_id"],
                f"❌ <b>Commande {num}</b> non aboutie.\n"
                "Contactez le support si vous avez envoyé de la crypto.",
                kb_accueil(),
            )
            return

        elif st == "partially_paid":
            cmd["etat"] = "probleme"
            db_maj(num, cmd)
            await modifier(
                bot, cmd["chat_id"], cmd["message_id"],
                f"⚠️ <b>Commande {num}</b> : paiement incomplet.\n"
                "Contactez le support avec votre numéro de commande et le TXID.",
                kb_accueil(),
            )
            await notifier_admin(bot, f"⚠️ {num} : paiement incomplet. À vérifier.")
            return

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        ACCUEIL, reply_markup=kb_accueil(), parse_mode="HTML"
    )
    if MINIAPP_URL:
        await update.message.reply_text(
            "📲 Ou ouvrez l’application d’échange avec le bouton ci-dessous.",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("📲 Ouvrir X_change", web_app=WebAppInfo(url=MINIAPP_URL))]],
                resize_keyboard=True,
            ),
        )

async def monid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Votre identifiant Telegram : {update.effective_user.id}"
    )

async def simuler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Utilisation : /simuler XC123456")
        return
    num = context.args[0].upper()
    commandes = context.application.bot_data.setdefault("commandes", {})
    cmd = obtenir_cmd(commandes, num)
    if not cmd or cmd["etat"] not in ("attente", "detecte"):
        await update.message.reply_text("Commande introuvable ou déjà traitée.")
        return
    await finaliser_paiement(context.bot, num, cmd, cmd["montant"])

async def boutons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    u = context.user_data
    commandes = context.application.bot_data.setdefault("commandes", {})

    if d == "accueil":
        u.clear()
        await q.edit_message_text(ACCUEIL, reply_markup=kb_accueil(), parse_mode="HTML")

    elif d == "annuler":
        u.clear()
        await q.edit_message_text(
            "❌ Opération annulée.\n\n" + ACCUEIL,
            reply_markup=kb_accueil(), parse_mode="HTML",
        )

    elif d == "vendre":
        u.clear()
        await q.edit_message_text(
            "<b>Quelle crypto voulez-vous vendre ?</b>",
            reply_markup=kb_cryptos(), parse_mode="HTML",
        )

    elif d in ("crypto_usdt_trc20", "crypto_usdt_bep20"):
        crypto = "USDT TRC20" if d == "crypto_usdt_trc20" else "USDT BEP20"
        u.clear()
        u["crypto"] = crypto
        u["etat"] = "montant"
        await q.edit_message_text(
            f"✅ Crypto choisie : <b>{crypto}</b>\n\n"
            "Entrez le <b>montant</b> à échanger (minimum 20 USDT).",
            reply_markup=kb_annuler(), parse_mode="HTML",
        )

    elif d in ("crypto_btc", "crypto_trx"):
        await q.edit_message_text(
            "⏳ Cette crypto sera bientôt disponible.\n"
            "Choisissez USDT pour le moment.",
            reply_markup=kb_cryptos(),
        )

    elif d == "aide" or d.startswith("probleme_"):
        u.clear()
        u["etat"] = "support"
        if d.startswith("probleme_"):
            u["ref"] = d[9:]
        await q.edit_message_text(
            "🆘 <b>Support X_change</b>\n"
            f"{SEP}\n"
            "Écrivez votre message ci-dessous. Indiquez votre "
            "<b>numéro de commande</b> et le <b>TXID</b> si possible.",
            reply_markup=kb_annuler(), parse_mode="HTML",
        )

    elif d == "continuer":
        if "montant" not in u:
            await q.edit_message_text("Session expirée. Tapez /start.")
            return
        await q.edit_message_text(
            "<b>Où voulez-vous recevoir votre argent ?</b>",
            reply_markup=kb_operateurs(), parse_mode="HTML",
        )

    elif d.startswith("op_"):
        if "montant" not in u:
            await q.edit_message_text("Session expirée. Tapez /start.")
            return
        u["operateur"] = d[3:]
        u["etat"] = "numero"
        await q.edit_message_text(
            f"✅ Réception sur <b>{u['operateur']}</b>\n\n"
            f"Entrez votre numéro <b>{u['operateur']}</b> "
            "(10 chiffres, commençant par 01).\n\n"
            f"⚠️ Ce doit être un numéro <b>{u['operateur']}</b>. "
            "Un numéro d'un autre réseau retardera votre paiement.",
            reply_markup=kb_annuler(), parse_mode="HTML",
        )

    elif d == "modifier_num":
        if "operateur" not in u:
            await q.edit_message_text("Session expirée. Tapez /start.")
            return
        u["etat"] = "numero"
        await q.edit_message_text(
            f"Entrez votre numéro <b>{u['operateur']}</b> "
            "(10 chiffres, commençant par 01).",
            reply_markup=kb_annuler(), parse_mode="HTML",
        )

    elif d == "espace":
        u.clear()
        texte_e, kb_e = construire_espace(q.from_user.id, q.from_user.full_name)
        await q.edit_message_text(texte_e, reply_markup=kb_e, parse_mode="HTML")

    elif d == "chercher":
        u.clear()
        u["etat"] = "chercher"
        await q.edit_message_text(
            "🔎 <b>Chercher une commande</b>\n\n"
            "Entrez le code de votre commande, par exemple <b>XC123456</b>.",
            reply_markup=clavier([[("⬅️ Mon espace", "espace")]]), parse_mode="HTML",
        )

    elif d.startswith("cmd_"):
        r = DB.execute(
            "SELECT * FROM commandes WHERE num=? AND user_id=?", (d[4:], q.from_user.id)
        ).fetchone()
        if not r:
            await q.edit_message_text(
                "Commande introuvable.",
                reply_markup=clavier([[("⬅️ Mon espace", "espace")]]),
            )
        else:
            await q.edit_message_text(
                texte_cmd(r), reply_markup=kb_detail(r), parse_mode="HTML",
            )

    elif d == "sans_email":
        if "nom" not in u:
            await q.edit_message_text("Session expirée. Tapez /start.")
            return
        u["email"] = None
        u["etat"] = None
        await q.edit_message_text(
            texte_verif(u), reply_markup=kb_confirmation(), parse_mode="HTML",
        )

    elif d == "confirmer":
        if not all(k in u for k in ("crypto", "montant", "operateur", "numero", "nom")):
            await q.edit_message_text("Session expirée. Tapez /start.")
            return
        crypto, m = u["crypto"], u["montant"]
        num = "XC" + str(random.randint(100000, 999999))
        try:
            info = await creer_paiement(m, crypto, num)
        except Exception as e:
            print("Erreur NOWPayments :", type(e).__name__)
            if isinstance(e, httpx.HTTPStatusError):
                print(e.response.text)
            await q.edit_message_text(
                "⚠️ Service de paiement momentanément indisponible.\n"
                "Réessayez dans quelques minutes.",
                reply_markup=kb_accueil(),
            )
            u.clear()
            return
        adresse = info["pay_address"]
        a_envoyer = info["pay_amount"]
        _, _, fcfa = calcul(m, crypto)
        reseau_nom = crypto.split()[1]
        commandes[num] = {
            "etat": "attente",
            "crypto": crypto,
            "montant": m,
            "operateur": u["operateur"],
            "numero": u["numero"],
            "nom": u["nom"],
            "email": u.get("email"),
            "payment_id": info["payment_id"],
            "chat_id": q.message.chat_id,
            "message_id": q.message.message_id,
            "user_id": q.from_user.id,
        }
        db_creer(num, commandes[num])
        await q.edit_message_text(
            f"✅ <b>Commande {num}</b>\n"
            f"{SEP}\n"
            f"Envoyez exactement <b>{a_envoyer} {crypto}</b> à cette adresse :\n"
            f"<code>{adresse}</code>\n\n"
            f"Vous recevrez <b>{fcfa_txt(fcfa)} FCFA</b> sur "
            f"{u['operateur']} au {u['numero']} ({u['nom']}).\n\n"
            f"⚠️ Envoyez <b>uniquement</b> du {crypto} sur le réseau "
            f"<b>{reseau_nom}</b>. Une mauvaise adresse ou un mauvais réseau "
            "peut entraîner la <b>perte définitive</b> de vos fonds.\n\n"
            "⏱ Vous avez <b>20 minutes</b>.",
            reply_markup=clavier([[("❌ Annuler la commande", "annulcmd_" + num)]]),
            parse_mode="HTML",
        )
        await notifier_admin(
            context.bot,
            f"🔔 ÉCHANGE LANCÉ {num}\n"
            f"{m:.2f} {crypto} → {fcfa_txt(fcfa)} FCFA\n"
            f"Réseau : {u['operateur']}\n"
            f"Numéro : {u['numero']}\n"
            f"Titulaire : {u['nom']}\n"
            "En attente du paiement…",
        )
        lancer_email(
            ADMIN_EMAIL,
            f"[X_change] Échange lancé {num}",
            f"ÉCHANGE LANCÉ - commande {num}\n\n"
            f"Montant : {m:.2f} {crypto}\n"
            f"À payer au client : {fcfa_txt(fcfa)} FCFA\n"
            f"Réseau : {u['operateur']}\n"
            f"Numéro : {u['numero']}\n"
            f"Titulaire : {u['nom']}\n"
            f"E-mail client : {u.get('email') or 'non renseigné'}\n"
            f"Adresse générée : {adresse}\n\n"
            "En attente du paiement du client.",
        )
        lancer_email(
            u.get("email"),
            f"X_change - Votre adresse de paiement ({num})",
            f"Bonjour,\n\n"
            f"Voici les détails de votre commande {num}.\n\n"
            f"Envoyez exactement : {a_envoyer} {crypto}\n"
            f"Adresse : {adresse}\n"
            f"Réseau : {reseau_nom}\n\n"
            f"Vous recevrez {fcfa_txt(fcfa)} FCFA sur {u['operateur']} "
            f"au {u['numero']} ({u['nom']}).\n\n"
            f"ATTENTION : envoyez uniquement du {crypto} sur le réseau {reseau_nom}. "
            "Une mauvaise adresse ou un mauvais réseau peut entraîner la perte "
            "définitive de vos fonds.\n"
            "Vous avez 20 minutes. Passé ce délai, n'envoyez plus rien à cette adresse.\n\n"
            "X_change",
        )
        tache = asyncio.create_task(surveiller(context.bot, num, commandes))
        TACHES.add(tache)
        tache.add_done_callback(TACHES.discard)
        u.clear()

    elif d.startswith("annulcmd_"):
        num = d[9:]
        cmd = obtenir_cmd(commandes, num)
        if not cmd or cmd["etat"] != "attente":
            return
        cmd["etat"] = "annulee"
        db_maj(num, cmd)
        u.clear()
        await q.edit_message_text(
            f"❌ <b>Commande {num} annulée.</b>\n"
            "N'envoyez pas de crypto à l'adresse indiquée.",
            reply_markup=kb_accueil(), parse_mode="HTML",
        )

    elif d.startswith("admpaye_"):
        if q.from_user.id != ADMIN_ID:
            return
        num = d[8:]
        cmd = obtenir_cmd(commandes, num)
        if not cmd or cmd["etat"] != "paye":
            return
        cmd["etat"] = "envoye"
        db_maj(num, cmd)
        await q.edit_message_text(
            (q.message.text or "") + "\n\n✅ Mobile Money marqué comme envoyé."
        )
        await modifier(
            context.bot, cmd["chat_id"], cmd["message_id"],
            f"💸 <b>Paiement envoyé — commande {num}</b>\n"
            f"{SEP}\n"
            f"<b>{fcfa_txt(cmd['fcfa'])} FCFA</b> ont été envoyés sur "
            f"{cmd['operateur']} au {cmd['numero']}.\n\n"
            "Avez-vous bien reçu votre argent ?",
            clavier([
                [("✅ J'ai reçu mon argent", "recu_" + num)],
                [("🆘 Un problème", "probleme_" + num)],
            ]),
        )

    elif d.startswith("recu_"):
        num = d[5:]
        cmd = obtenir_cmd(commandes, num)
        if not cmd or cmd["etat"] != "envoye":
            await q.edit_message_text(
                "Cette commande est déjà terminée.", reply_markup=kb_accueil()
            )
            return
        cmd["etat"] = "cloturee"
        db_maj(num, cmd)
        await q.edit_message_text(
            f"🎉 <b>Merci !</b>\n"
            f"Commande {num} terminée. À bientôt sur X_change.",
            reply_markup=clavier([[("💱 Nouvelle demande d'échange", "vendre")]]),
            parse_mode="HTML",
        )
        await notifier_admin(
            context.bot,
            f"🎉 COMMANDE TERMINÉE {num}\n"
            f"Le client confirme avoir reçu {fcfa_txt(cmd['fcfa'])} FCFA.",
        )

async def texte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = context.user_data
    uid = update.effective_user.id
    saisie = update.message.text.strip()

    if uid == ADMIN_ID and update.message.reply_to_message:
        support = context.application.bot_data.get("support", {})
        cible = support.get(update.message.reply_to_message.message_id)
        if cible:
            try:
                await context.bot.send_message(
                    chat_id=cible,
                    text="💬 Réponse du support X_change :\n\n" + saisie,
                )
                await update.message.reply_text("✅ Réponse envoyée au client.")
            except Exception:
                await update.message.reply_text("❌ Envoi impossible.")
            return

    etat = u.get("etat")

    if etat == "support":
        ref = u.get("ref", "aucune")
        nom = update.effective_user.full_name
        msg = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🆘 SUPPORT\n"
                f"Commande : {ref}\n"
                f"Client : {nom} (id {uid})\n\n"
                f"{saisie}\n\n"
                "↩️ Répondez à ce message pour répondre au client."
            ),
        )
        context.application.bot_data.setdefault("support", {})[msg.message_id] = (
            update.effective_chat.id
        )
        u.clear()
        await update.message.reply_text(
            "✅ Message envoyé au support. Nous vous répondons rapidement.",
            reply_markup=kb_accueil(),
        )

    elif etat == "montant":
        try:
            m = float(saisie.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Entrez un nombre, par exemple 50.")
            return
        if m < MINIMUM:
            await update.message.reply_text(
                "❌ Le minimum est de 20 USDT. Entrez un montant plus élevé."
            )
            return
        crypto = u["crypto"]
        frais, reseau, fcfa = calcul(m, crypto)
        u["montant"] = m
        u["etat"] = None
        await update.message.reply_text(
            "📋 <b>Récapitulatif</b>\n"
            f"{SEP}\n"
            f"Vous envoyez : <b>{m:.2f} {crypto}</b>\n"
            f"Frais de service (3 %) : {frais:.2f} USDT\n"
            f"Frais de réseau : {reseau:.2f} USDT\n"
            f"Taux : 1 USDT = {TAUX_FCFA} FCFA\n"
            f"{SEP}\n"
            f"💰 Vous recevez : <b>{fcfa_txt(fcfa)} FCFA</b>",
            reply_markup=kb_recap(), parse_mode="HTML",
        )

    elif etat == "numero":
        chiffres = re.sub(r"\D", "", saisie)
        if chiffres.startswith("229") and len(chiffres) == 13:
            chiffres = chiffres[3:]
        if len(chiffres) == 8:
            chiffres = "01" + chiffres
        if len(chiffres) != 10 or not chiffres.startswith("01"):
            await update.message.reply_text(
                "❌ Numéro invalide. Entrez 10 chiffres commençant par 01, "
                "par exemple 01XXXXXXXX."
            )
            return
        u["numero"] = chiffres
        u["etat"] = "nom"
        await update.message.reply_text(
            f"✅ Numéro enregistré : <b>{chiffres}</b>\n\n"
            f"Entrez maintenant le <b>nom et prénom du titulaire</b> du compte "
            f"{u['operateur']}.\n"
            "⚠️ Ils doivent correspondre <b>exactement</b> au nom du compte.",
            reply_markup=kb_annuler(), parse_mode="HTML",
        )

    elif etat == "nom":
        nom = " ".join(saisie.split())
        valide = re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ'’\- ]{3,60}", nom) and len(nom.split()) >= 2
        if not valide:
            await update.message.reply_text(
                "❌ Entrez votre nom <b>et</b> votre prénom (lettres uniquement), "
                "par exemple : Koffi Adjovi.",
                parse_mode="HTML",
            )
            return
        u["nom"] = nom.upper()
        u["etat"] = "email"
        await update.message.reply_text(
            "📧 <b>E-mail (facultatif)</b>\n\n"
            "Entrez votre e-mail pour recevoir <b>immédiatement</b> "
            "l'adresse de paiement par message.\n"
            "Sinon, appuyez sur <b>Passer</b>.",
            reply_markup=kb_passer_email(), parse_mode="HTML",
        )

    elif etat == "email":
        mail = saisie.strip()
        if not re.fullmatch(REGEX_EMAIL, mail):
            await update.message.reply_text(
                "❌ E-mail invalide. Exemple : nom@gmail.com\n"
                "Ou appuyez sur Passer.",
                reply_markup=kb_passer_email(),
            )
            return
        u["email"] = mail.lower()
        u["etat"] = None
        await update.message.reply_text(
            texte_verif(u), reply_markup=kb_confirmation(), parse_mode="HTML",
        )

    elif etat == "chercher":
        code = saisie.upper().replace(" ", "")
        if not re.fullmatch(r"XC\d{6}", code):
            await update.message.reply_text(
                "❌ Code invalide. Exemple : XC123456",
                reply_markup=clavier([[("⬅️ Mon espace", "espace")]]),
            )
            return
        r = DB.execute(
            "SELECT * FROM commandes WHERE num=? AND user_id=?", (code, uid)
        ).fetchone()
        if not r:
            await update.message.reply_text(
                "❌ Aucune commande avec ce code dans votre espace.",
                reply_markup=clavier([[("⬅️ Mon espace", "espace")]]),
            )
            return
        u.clear()
        await update.message.reply_text(
            texte_cmd(r), reply_markup=kb_detail(r), parse_mode="HTML",
        )

    else:
        await update.message.reply_text("Utilisez les boutons, ou tapez /start.")

async def miniapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg0 = update.message
    try:
        d = json.loads(msg0.web_app_data.data)
        crypto = d["crypto"]
        operateur = d["operateur"]
        m = round(float(d["montant"]), 2)
        chiffres = re.sub(r"\D", "", str(d["numero"]))
        nom = " ".join(str(d["nom"]).split())
        email = d.get("email")
    except Exception:
        await msg0.reply_text("❌ Données invalides. Réessayez avec /start.")
        return
    if chiffres.startswith("229") and len(chiffres) == 13:
        chiffres = chiffres[3:]
    if len(chiffres) == 8:
        chiffres = "01" + chiffres
    if email:
        email = str(email).strip().lower()
        if not re.fullmatch(REGEX_EMAIL, email):
            email = None
    erreur = None
    if crypto not in CODES or operateur not in ("MTN", "Moov", "Celtis"):
        erreur = "Crypto ou réseau invalide."
    elif not (MINIMUM <= m <= MAXIMUM):
        erreur = "Montant invalide (minimum 20 USDT)."
    elif len(chiffres) != 10 or not chiffres.startswith("01"):
        erreur = "Numéro invalide."
    elif not (re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ'’\- ]{3,60}", nom) and len(nom.split()) >= 2):
        erreur = "Nom invalide."
    if erreur:
        await msg0.reply_text("❌ " + erreur + " Réessayez avec /start.")
        return
    nom = nom.upper()
    num = "XC" + str(random.randint(100000, 999999))
    try:
        info = await creer_paiement(m, crypto, num)
    except Exception as e:
        print("Erreur NOWPayments :", type(e).__name__)
        if isinstance(e, httpx.HTTPStatusError):
            print(e.response.text)
        await msg0.reply_text(
            "⚠️ Service de paiement momentanément indisponible.\n"
            "Réessayez dans quelques minutes.",
            reply_markup=kb_accueil(),
        )
        return
    adresse = info["pay_address"]
    a_envoyer = info["pay_amount"]
    _, _, fcfa = calcul(m, crypto)
    reseau_nom = crypto.split()[1]
    envoye = await msg0.reply_text(
        f"✅ <b>Commande {num}</b>\n"
        f"{SEP}\n"
        f"Envoyez exactement <b>{a_envoyer} {crypto}</b> à cette adresse :\n"
        f"<code>{adresse}</code>\n\n"
        f"Vous recevrez <b>{fcfa_txt(fcfa)} FCFA</b> sur "
        f"{operateur} au {chiffres} ({nom}).\n\n"
        f"⚠️ Envoyez <b>uniquement</b> du {crypto} sur le réseau "
        f"<b>{reseau_nom}</b>. Une mauvaise adresse ou un mauvais réseau "
        "peut entraîner la <b>perte définitive</b> de vos fonds.\n\n"
        "⏱ Vous avez <b>20 minutes</b>.",
        reply_markup=clavier([[("❌ Annuler la commande", "annulcmd_" + num)]]),
        parse_mode="HTML",
    )
    commandes = context.application.bot_data.setdefault("commandes", {})
    commandes[num] = {
        "etat": "attente",
        "crypto": crypto,
        "montant": m,
        "operateur": operateur,
        "numero": chiffres,
        "nom": nom,
        "email": email,
        "payment_id": info["payment_id"],
        "chat_id": envoye.chat_id,
        "message_id": envoye.message_id,
        "user_id": update.effective_user.id,
    }
    db_creer(num, commandes[num])
    await notifier_admin(
        context.bot,
        f"🔔 ÉCHANGE LANCÉ {num} (Mini App)\n"
        f"{m:.2f} {crypto} → {fcfa_txt(fcfa)} FCFA\n"
        f"Réseau : {operateur}\n"
        f"Numéro : {chiffres}\n"
        f"Titulaire : {nom}\n"
        "En attente du paiement…",
    )
    lancer_email(
        ADMIN_EMAIL,
        f"[X_change] Échange lancé {num}",
        f"ÉCHANGE LANCÉ - commande {num}\n\n"
        f"Montant : {m:.2f} {crypto}\n"
        f"À payer au client : {fcfa_txt(fcfa)} FCFA\n"
        f"Réseau : {operateur}\n"
        f"Numéro : {chiffres}\n"
        f"Titulaire : {nom}\n"
        f"E-mail client : {email or 'non renseigné'}\n"
        f"Adresse générée : {adresse}\n\n"
        "En attente du paiement du client.",
    )
    lancer_email(
        email,
        f"X_change - Votre adresse de paiement ({num})",
        f"Bonjour,\n\n"
        f"Voici les détails de votre commande {num}.\n\n"
        f"Envoyez exactement : {a_envoyer} {crypto}\n"
        f"Adresse : {adresse}\n"
        f"Réseau : {reseau_nom}\n\n"
        f"Vous recevrez {fcfa_txt(fcfa)} FCFA sur {operateur} "
        f"au {chiffres} ({nom}).\n\n"
        f"ATTENTION : envoyez uniquement du {crypto} sur le réseau {reseau_nom}. "
        "Une mauvaise adresse ou un mauvais réseau peut entraîner la perte "
        "définitive de vos fonds.\n"
        "Vous avez 20 minutes. Passé ce délai, n'envoyez plus rien à cette adresse.\n\n"
        "X_change",
    )
    tache = asyncio.create_task(surveiller(context.bot, num, commandes))
    TACHES.add(tache)
    tache.add_done_callback(TACHES.discard)

async def espace_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texte_e, kb_e = construire_espace(
        update.effective_user.id, update.effective_user.full_name
    )
    await update.message.reply_text(texte_e, reply_markup=kb_e, parse_mode="HTML")

async def commande_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Utilisation : /commande XC123456")
        return
    r = DB.execute(
        "SELECT * FROM commandes WHERE num=?", (context.args[0].upper(),)
    ).fetchone()
    if not r:
        await update.message.reply_text("Commande introuvable.")
        return
    await update.message.reply_text(
        texte_cmd(r) + f"\n\nClient : id {r['user_id']}", parse_mode="HTML"
    )

async def reprendre(application):
    commandes = application.bot_data.setdefault("commandes", {})
    limite = int(time.time()) - (DELAI_SECONDES + SURVEILLANCE_APRES)
    rows = DB.execute(
        "SELECT num FROM commandes WHERE etat IN ('attente','detecte','annulee','expiree') "
        "AND cree_le > ?", (limite,)
    ).fetchall()
    for r in rows:
        cmd = obtenir_cmd(commandes, r["num"])
        t = asyncio.create_task(
            surveiller(application.bot, r["num"], commandes, cmd["debut"])
        )
        TACHES.add(t)
        t.add_done_callback(TACHES.discard)
    if rows:
        print(len(rows), "commande(s) reprise(s) après redémarrage.")

app = Application.builder().token(os.environ["BOT_TOKEN"]).post_init(reprendre).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("monid", monid))
app.add_handler(CommandHandler("simuler", simuler))
app.add_handler(CommandHandler(["historique", "espace"], espace_cmd))
app.add_handler(CommandHandler("commande", commande_admin))
app.add_handler(CallbackQueryHandler(boutons))
app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, miniapp))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texte))
app.run_polling()
