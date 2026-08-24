"""Panel giriş kontrolü — yetkisiz erişimi engeller."""
import streamlit as st

_PANELS = {
    "mobil": "📱 Mobil Panel",
    "servis": "🚐 Servis Paneli",
    "admin": "🔐 Admin Panel",
}


def require_auth(panel: str) -> None:
    """Şifre doğrulanmadan panel içeriğini göstermez."""
    if panel not in _PANELS:
        raise ValueError(f"Bilinmeyen panel: {panel}")

    session_key = f"auth_ok_{panel}"
    if st.session_state.get(session_key):
        return

    auth = st.secrets.get("auth", {})
    secret_key = f"{panel}_password"
    if secret_key not in auth:
        st.error("Panel şifresi yapılandırılmamış. Streamlit Secrets → [auth] bölümünü doldurun.")
        st.stop()

    st.markdown(f"### {_PANELS[panel]}")
    st.caption("Devam etmek için şifre girin.")
    pwd = st.text_input("Şifre", type="password", key=f"login_pwd_{panel}")
    if st.button("Giriş", type="primary", use_container_width=True, key=f"login_btn_{panel}"):
        if pwd == auth[secret_key]:
            st.session_state[session_key] = True
            st.rerun()
        st.error("Hatalı şifre.")
    st.stop()
