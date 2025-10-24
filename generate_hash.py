import streamlit_authenticator as stauth

# 新しいパスワードをここに設定
password = "123"

# v0.4.2 では Hasher() → .hash() を使用
hasher = stauth.Hasher()
hashed_pw = hasher.hash(password)

print("🔒 ハッシュ化されたパスワード:")
print(hashed_pw)