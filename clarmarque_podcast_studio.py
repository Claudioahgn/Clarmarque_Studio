import io
import json
import mimetypes
import re
import struct
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

CLONED_VOICE_NAME = "Ma Voix"

# ==========================================
# GESTION DE LA MÉMOIRE (SESSION STATE)
# ==========================================
if "blocs_script" not in st.session_state:
    st.session_state.blocs_script = None

def reinitialiser_session():
    st.session_state.blocs_script = None

# ==========================================
# FONCTIONS AUDIO (CONVERSION WAV OFFICIELLE AI STUDIO)
# ==========================================
def parse_audio_mime_type(mime_type: str) -> dict:
    bits_per_sample = 16
    rate = 24000
    parts = mime_type.split(";")
    for param in parts:
        param = param.strip()
        if param.lower().startswith("rate="):
            try:
                rate = int(param.split("=", 1)[1])
            except (ValueError, IndexError):
                pass
        elif param.startswith("audio/L"):
            try:
                bits_per_sample = int(param.split("L", 1)[1])
            except (ValueError, IndexError):
                pass
    return {"bits_per_sample": bits_per_sample, "rate": rate}

def convert_to_wav(audio_data: bytes, mime_type: str) -> bytes:
    parameters = parse_audio_mime_type(mime_type)
    bits_per_sample = parameters["bits_per_sample"]
    sample_rate = parameters["rate"]
    num_channels = 1
    data_size = len(audio_data)
    bytes_per_sample = bits_per_sample // 8
    block_align = num_channels * bytes_per_sample
    byte_rate = sample_rate * block_align
    chunk_size = 36 + data_size

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        chunk_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size
    )
    return header + audio_data

# ==========================================
# FONCTIONS UTILITAIRES (CLÉS, EXTRACTION, NETTOYAGE)
# ==========================================
def assainir_cle(cle_brute: str) -> str:
    return (
        cle_brute.replace(r'\_', '_')
        .replace('\\', '')
        .strip()
        .strip('"')
        .strip("'")
    )

def extraire_texte_fichier(fichier_telecharge) -> str:
    nom = fichier_telecharge.name.lower()
    if nom.endswith(".txt"):
        return fichier_telecharge.read().decode("utf-8", errors="ignore").strip()
    elif nom.endswith(".pdf"):
        doc = fitz.open(stream=fichier_telecharge.read(), filetype="pdf")
        return "\n\n".join(page.get_text() for page in doc).strip()
    return ""

def nettoyer_pour_tts(texte: str) -> str:
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
# MOTEURS IA (TEXTE & AUDIO V1ALPHA)
# ==========================================
def executer_requete_ia(pool_cles: list, fonction_appel, *args, api_version: str = None):
    if not pool_cles:
        raise ValueError("Aucune clé API valide configurée.")

    for idx, cle in enumerate(pool_cles):
        try:
            if api_version:
                client = genai.Client(api_key=cle, http_options={"api_version": api_version})
            else:
                client = genai.Client(api_key=cle)
            return fonction_appel(client, *args)
        except Exception as e:
            if idx + 1 < len(pool_cles):
                st.warning(f"⚠️ Incident sur la clé #{idx + 1} ({str(e)[:80]}). Bascule sur la clé #{idx + 2}...")
                time.sleep(1)
            else:
                raise RuntimeError(f"Erreur API : {e}")

def generer_adaptation_storybrand(client: genai.Client, texte_source: str, modele_texte: str) -> list:
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

def synthetiser_bloc_audio(client: genai.Client, texte: str, voice_name: str, modele_tts: str) -> bytes:
    """Génère la piste audio WAV via generate_content_stream sur l'endpoint v1alpha."""
    texte_propre = nettoyer_pour_tts(texte)
    
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=f"## Transcript:\n{texte_propre}"),
            ],
        ),
    ]
    
    generate_content_config = types.GenerateContentConfig(
        temperature=0.35,
        response_modalities=["audio"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice_name
                )
            )
        ),
    )

    audio_data = bytearray()
    mime_type = "audio/L16;rate=24000"

    for chunk in client.models.generate_content_stream(
        model=modele_tts,
        contents=contents,
        config=generate_content_config,
    ):
        if chunk.parts is None:
            continue
        if chunk.parts[0].inline_data and chunk.parts[0].inline_data.data:
            inline_data = chunk.parts[0].inline_data
            audio_data.extend(inline_data.data)
            if inline_data.mime_type:
                mime_type = inline_data.mime_type

    if not audio_data:
        raise ValueError("Aucun flux audio n'a été retourné par le modèle TTS.")

    file_extension = mimetypes.guess_extension(mime_type)
    data_buffer = bytes(audio_data)
    if file_extension is None:
        data_buffer = convert_to_wav(data_buffer, mime_type)

    return data_buffer

# ==========================================
# INTERFACE UTILISATEUR STREAMLIT
# ==========================================
def main():
    st.title("🎙️ Clarmarque Podcast Studio")
    st.markdown("Production éditoriale & synthèse vocale clonée (**Claudio AHN**) prête pour **Adobe Audition**.")
    st.divider()

    cles_brutes = st.secrets.get("GOOGLE_API_KEYS", None)
    if cles_brutes is None:
        cle_solo = st.secrets.get("GOOGLE_API_KEY", "")
        pool_initial = [cle_solo] if cle_solo else []
    else:
        pool_initial = list(cles_brutes)
    pool_cles = [assainir_cle(k) for k in pool_initial if assainir_cle(k)]

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
        voice_name = st.text_input("Nom Voix Clonée (AI Studio)", value=CLONED_VOICE_NAME)
        api_ver_tts = st.selectbox("Version API TTS", ["v1alpha", "v1beta"], index=0)

        st.divider()
        if st.button("🔊 Tester ma voix (5 sec)", use_container_width=True):
            with st.spinner("Test de la voix clonée..."):
                try:
                    test_wav = executer_requete_ia(
                        pool_cles,
                        synthetiser_bloc_audio,
                        "Bonjour, ici Claudio AHN. Bienvenue sur Clarmarque.",
                        voice_name,
                        modele_tts,
                        api_version=api_ver_tts
                    )
                    st.audio(test_wav, format="audio/wav")
                except Exception as e:
                    st.error(str(e))

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
                            voice_name,
                            modele_tts,
                            api_version=api_ver_tts
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
