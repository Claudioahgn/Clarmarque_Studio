import io
import json
import re
import time
import zipfile
import fitz  # PyMuPDF
from google import genai
from google.genai import types
import streamlit as st

# ==========================================
# CONFIGURATION DE LA PAGE & PARAMÈTRES CLARMARQUE
# ==========================================
st.set_page_config(
    page_title="Clarmarque Podcast Studio — Claudio AHN",
    page_icon="🎙️",
    layout="wide"
)

# ID de ta voix clonée sur Google AI Studio ("Ma Voix")
CUSTOM_VOICE_ID = "voice_tlmpjv7km4sh"

# Verrouillage prosodique pour éviter toute dérive d'accent ou de débit
VOICE_STYLE_LOCK = (
    "Langue : Français. Posture : Architecte de marque pédagogue, posé, chaleureux et assuré. "
    "Débit régulier d'atelier (135 mots/min), articulation nette, ton naturel et conversationnel. "
    "Conserve exactement la même hauteur tonale et le même accent sur tout le texte."
)

# ==========================================
# GESTION DE LA MÉMOIRE (SESSION STATE)
# ==========================================
if "blocs_script" not in st.session_state:
    st.session_state.blocs_script = None

def reinitialiser_session():
    st.session_state.blocs_script = None

# ==========================================
# FONCTIONS UTILITAIRES (CLÉS, EXTRACTION, NETTOYAGE)
# ==========================================
def assainir_cle(cle_brute: str) -> str:
    """Élimine les antislashes Markdown, espaces ou guillemets parasites."""
    return (
        cle_brute.replace(r'\_', '_')
        .replace('\\', '')
        .strip()
        .strip('"')
        .strip("'")
    )

def extraire_texte_fichier(fichier_telecharge) -> str:
    """Extrait proprement le texte d'un .txt ou .pdf via PyMuPDF."""
    nom = fichier_telecharge.name.lower()
    if nom.endswith(".txt"):
        return fichier_telecharge.read().decode("utf-8", errors="ignore").strip()
    elif nom.endswith(".pdf"):
        doc = fitz.open(stream=fichier_telecharge.read(), filetype="pdf")
        return "\n\n".join(page.get_text() for page in doc).strip()
    return ""

def nettoyer_pour_tts(texte: str) -> str:
    """Supprime les symboles lus à voix haute (*, #, _) et corrige les références chiffrées."""
    if not texte:
        return ""
    texte = re.sub(r'(\d+):(\d+)', r'\1, \2', texte)
    texte = texte.replace("*", "").replace("_", "")
    texte = re.sub(r'^#+\s*', '', texte, flags=re.MULTILINE)
    texte = re.sub(r'\s*[—–]\s*', ', ', texte)
    texte = texte.replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"')
    texte = re.sub(r'[ \t]+', ' ', texte)
    texte = re.sub(r'\n{3,}', '\n\n', texte)
    return texte.strip()

# ==========================================
# MOTEURS IA (CLÉ PAYANTE + FAILOVER DE SECOURS)
# ==========================================
def executer_requete_ia(pool_cles: list, fonction_appel, *args):
    """Exécute l'appel API avec la clé principale (payante) et bascule si nécessaire."""
    if not pool_cles:
        raise ValueError("Aucune clé API valide configurée.")

    for idx, cle in enumerate(pool_cles):
        try:
            client = genai.Client(api_key=cle)
            return fonction_appel(client, *args)
        except Exception as e:
            if idx + 1 < len(pool_cles):
                st.warning(f"⚠️ Incident sur la clé #{idx + 1} ({str(e)[:80]}). Bascule sur la clé #{idx + 2}...")
                time.sleep(1)
            else:
                raise RuntimeError(f"Erreur API : {e}")

def generer_adaptation_storybrand(client: genai.Client, texte_source: str, modele_texte: str) -> list:
    """Adapte le podcast de référence au positionnement Clarmarque (Claudio AHN uniquement)."""
    system_prompt = """
    Tu es l'architecte éditorial du podcast 'Clarmarque' animé par Claudio AHN.
    Adapte la transcription de référence fournie au marché francophone :
    - Posture d'artisan-bâtisseur et maître d'œuvre (pragmatisme, bon sens opérationnel, zéro jargon d'agence).
    - Remplace les anecdotes/métaphores américaines par des réalités francophones concrètes.
    - Accorde un crédit respectueux à Donald Miller comme mentor intellectuel, tout en affirmant l'expertise terrain de Claudio AHN.
    - Génère UNIQUEMENT les interventions de Claudio AHN (les jingles et capsules Voix Off sont déjà montés à part dans Adobe Audition).
    - N'utilise AUCUN balisage Markdown (*, #, _) dans les textes générés.
    
    Découpe le monologue en 4 séquences logiques :
    1. "01_Accroche_Ouverture"
    2. "02_Developpement_Partie_1" (avant la coupure pub du hub clarmarque.com)
    3. "03_Developpement_Partie_2" (après la coupure)
    4. "04_Synthese_Defi_Semaine"
    
    Réponds STRICTEMENT au format JSON :
    [{"sequence": "01_Accroche_Ouverture", "texte": "..."}, ...]
    """
    reponse = client.models.generate_content(
        model=modele_texte,
        contents=f"Voici la transcription de référence à adapter :\n\n{texte_source}",
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.45,
            response_mime_type="application/json"
        )
    )
    return json.loads(reponse.text)

def synthetiser_bloc_audio(client: genai.Client, texte: str, voice_id: str, seed_val: int, modele_tts: str) -> bytes:
    """Génère la piste audio WAV avec la voix clonée de Claudio AHN."""
    texte_propre = nettoyer_pour_tts(texte)
    prompt_tts = f"[Style: {VOICE_STYLE_LOCK}]\n\n{texte_propre}"
    
    reponse = client.models.generate_content(
        model=modele_tts,
        contents=prompt_tts,
        config=types.GenerateContentConfig(
            temperature=0.2,
            seed=int(seed_val),
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_id
                    )
                )
            )
        )
    )
    return reponse.candidates[0].content.parts[0].inline_data.data

# ==========================================
# INTERFACE UTILISATEUR STREAMLIT
# ==========================================
def main():
    st.title("🎙️ Clarmarque Podcast Studio")
    st.markdown("Production éditoriale & synthèse vocale clonée (**Claudio AHN**) prête pour **Adobe Audition**.")
    st.divider()

    # Chargement de la clé API payante depuis les Secrets Streamlit
    cles_brutes = st.secrets.get("GOOGLE_API_KEYS", None)
    if cles_brutes is None:
        cle_solo = st.secrets.get("GOOGLE_API_KEY", "")
        pool_initial = [cle_solo] if cle_solo else []
    else:
        pool_initial = list(cles_brutes)
    pool_cles = [assainir_cle(k) for k in pool_initial if assainir_cle(k)]

    # Barre latérale de configuration
    with st.sidebar:
        st.header("⚙️ Studio & Voix Clonée")
        st.caption(f"💳 **{len(pool_cles)} clé(s) API** active(s).")
        cle_manuelle = st.text_input("Clé API manuelle (optionnel) :", type="password")
        if cle_manuelle.strip():
            pool_cles = [assainir_cle(cle_manuelle)]

        modele_texte = st.selectbox(
            "Modèle d'écriture éditoriale",
            ["gemini-3.8-flash", "gemini-3.1-pro-preview", "gemini-3.7-flash"]
        )
        modele_tts = st.text_input("Modèle TTS", value="gemini-3.8-flash-tts")
        voice_id = st.text_input("ID Voix Clonée (Claudio AHN)", value=CUSTOM_VOICE_ID)
        seed_vocal = st.number_input("Seed vocal (verrouillage timbre)", value=42, step=1)

        st.info("⚠️ Assure-toi que la clé API provient du même projet AI Studio où la voix a été clonée.")

        if st.button("🔄 Nouvel épisode", use_container_width=True):
            reinitialiser_session()
            st.rerun()

    # ÉTAPE 1 : INGESTION DE LA SOURCE
    st.subheader("1. Importer le podcast de référence (EN et/ou FR)")
    col1, col2 = st.columns(2)
    with col1:
        fichier_source = st.file_uploader("Fichier source (.txt ou .pdf)", type=["txt", "pdf"])
    with col2:
        texte_colle = st.text_area("Ou coller les transcriptions directement :", height=140)

    texte_reference = ""
    if fichier_source is not None:
        texte_reference = extraire_texte_fichier(fichier_source)
    elif texte_colle.strip():
        texte_reference = texte_colle.strip()

    if texte_reference:
        st.success(f"✅ Source chargée ({len(texte_reference)} caractères).")
        if st.button("✨ Générer le script Clarmarque", type="primary"):
            with st.spinner("Rédaction et adaptation StoryBrand en cours..."):
                try:
                    blocs = executer_requete_ia(pool_cles, generer_adaptation_storybrand, texte_reference, modele_texte)
                    for b in blocs:
                        b["texte"] = nettoyer_pour_tts(b["texte"])
                    st.session_state.blocs_script = blocs
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    # ÉTAPE 2 : ATELIER DE RETOUCHE HUMAINE
    if st.session_state.blocs_script is not None:
        st.divider()
        st.subheader("2. Atelier de Retouche (Touche personnelle)")
        st.info("Relisez et injectez vos anecdotes terrain avant de lancer l'enregistrement avec votre voix clonée.")

        blocs_valides = []
        for idx, bloc in enumerate(st.session_state.blocs_script):
            with st.expander(f"📝 Séquence : {bloc['sequence']}", expanded=True):
                texte_modifie = st.text_area(
                    "Texte à vocaliser :",
                    value=bloc["texte"],
                    height=200,
                    key=f"seq_{idx}"
                )
                blocs_valides.append({"sequence": bloc["sequence"], "texte": texte_modifie})

        script_complet_txt = "\n\n".join([f"=== {b['sequence']} ===\n{b['texte']}" for b in blocs_valides])
        st.download_button(
            label="📄 Télécharger le script validé (.txt)",
            data=script_complet_txt,
            file_name="Script_Clarmarque_Valide.txt",
            mime="text/plain"
        )

        # ÉTAPE 3 : ENREGISTREMENT AVEC LA VOIX CLONÉE
        st.divider()
        st.subheader("3. Enregistrement Studio (Voix Clonée Claudio AHN)")
        if st.button("🎧 Générer les pistes audio WAV", type="primary"):
            zip_buffer = io.BytesIO()
            barre = st.progress(0, text="Connexion à votre empreinte vocale...")

            try:
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for idx, bloc in enumerate(blocs_valides):
                        pct = int(((idx + 1) / len(blocs_valides)) * 100)
                        barre.progress(pct, text=f"Enregistrement de {bloc['sequence']} ({idx + 1}/{len(blocs_valides)})...")

                        audio_bytes = executer_requete_ia(
                            pool_cles,
                            synthetiser_bloc_audio,
                            bloc["texte"],
                            voice_id,
                            seed_vocal,
                            modele_tts
                        )

                        nom_fichier = f"{bloc['sequence']}_Claudio.wav"
                        zf.writestr(nom_fichier, audio_bytes)

                        st.markdown(f"**{nom_fichier}**")
                        st.audio(audio_bytes, format="audio/wav")

                st.success("🎉 Les 4 pistes de Claudio AHN sont prêtes à glisser dans Adobe Audition !")
                st.download_button(
                    label="📥 Télécharger le pack audio (.ZIP)",
                    data=zip_buffer.getvalue(),
                    file_name="Clarmarque_Pistes_Claudio.zip",
                    mime="application/zip",
                    type="primary"
                )
            except Exception as e:
                st.error(str(e))

if __name__ == "__main__":
    main()
