//! i18n — chuỗi giao diện người dùng Việt/Anh.
//!
//! Single source cho mọi text gửi user. Admin command vẫn tiếng Anh (operator).
//! `Lang` lưu trong Settings store theo user_id (bảng `user_lang`).

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lang {
    Vi,
    En,
}

impl Lang {
    pub fn from_code(s: &str) -> Option<Lang> {
        match s.trim().to_lowercase().as_str() {
            "vi" => Some(Lang::Vi),
            "en" => Some(Lang::En),
            _ => None,
        }
    }
    pub fn code(self) -> &'static str {
        match self {
            Lang::Vi => "vi",
            Lang::En => "en",
        }
    }
}

/// Chọn theo lang: (vi, en).
fn pick(lang: Lang, vi: &str, en: &str) -> String {
    match lang {
        Lang::Vi => vi.to_string(),
        Lang::En => en.to_string(),
    }
}

// ─── Language picker ────────────────────────────────────────────────────

pub fn choose_language() -> &'static str {
    "🌐 Chọn ngôn ngữ / Choose your language:"
}

pub fn language_set(lang: Lang) -> String {
    pick(lang, "✅ Đã đặt ngôn ngữ: Tiếng Việt", "✅ Language set: English")
}

// ─── Welcome / help ───────────────────────────────────────────────────

pub fn welcome(lang: Lang) -> String {
    pick(
        lang,
        "👋 *UPI QR Bot*\n\n\
         Gửi cho bot 1 trong 2:\n\
         • File `session.json` lấy từ https://chatgpt.com/api/auth/session\n\
         • Hoặc 1 tài khoản dạng `email|password|2fa_secret`\n\n\
         Bạn chạy được tối đa *2 tiến trình* cùng lúc. Mỗi tiến trình hiển thị ở 1 tin riêng kèm nút Dừng.\n\n\
         Chọn thao tác:",
        "👋 *UPI QR Bot*\n\n\
         Send the bot one of:\n\
         • A `session.json` file from https://chatgpt.com/api/auth/session\n\
         • Or one account as `email|password|2fa_secret`\n\n\
         You can run up to *2 processes* at once. Each process shows in its own message with a Stop button.\n\n\
         Pick an action:",
    )
}

// ─── Button labels ──────────────────────────────────────────────────────

pub fn btn_status(lang: Lang) -> String {
    pick(lang, "📊 Trạng thái", "📊 Status")
}
pub fn btn_stop_all(lang: Lang) -> String {
    pick(lang, "🛑 Dừng hết", "🛑 Stop all")
}
pub fn btn_help(lang: Lang) -> String {
    pick(lang, "❓ Trợ giúp", "❓ Help")
}
pub fn btn_settings(lang: Lang) -> String {
    pick(lang, "⚙️ Cài đặt", "⚙️ Settings")
}
pub fn btn_stop_this(lang: Lang) -> String {
    pick(lang, "🛑 Dừng tiến trình này", "🛑 Stop this process")
}
pub fn btn_language(lang: Lang) -> String {
    pick(lang, "🌐 Ngôn ngữ", "🌐 Language")
}

// ─── Settings menu ────────────────────────────────────────────────────

pub fn settings_title(lang: Lang) -> String {
    pick(lang, "⚙️ Cài đặt", "⚙️ Settings")
}

// ─── Submit / validation ────────────────────────────────────────────────

pub fn invalid_session_json(lang: Lang) -> String {
    pick(
        lang,
        "❌ JSON không hợp lệ. Chỉ nhận file `session.json` lấy từ \
         https://chatgpt.com/api/auth/session (phải có `accessToken` + `user`), \
         hoặc combo `email|password|2fa_secret`.",
        "❌ Invalid JSON. Only accept `session.json` from \
         https://chatgpt.com/api/auth/session (must contain `accessToken` + `user`), \
         or combo `email|password|2fa_secret`.",
    )
}

pub fn invalid_combo(lang: Lang) -> String {
    pick(
        lang,
        "❌ Sai định dạng. Gửi 1 tài khoản mỗi lần dạng `email|password|2fa_secret`, \
         hoặc gửi file `session.json`.",
        "❌ Wrong format. Send one account at a time as `email|password|2fa_secret`, \
         or send a `session.json` file.",
    )
}

pub fn need_input(lang: Lang) -> String {
    pick(
        lang,
        "📄 Gửi file `session.json` hoặc dán combo `email|password|2fa`.",
        "📄 Send a `session.json` file or paste combo `email|password|2fa`.",
    )
}

// ─── Admission ────────────────────────────────────────────────────────

pub fn max_concurrent(lang: Lang, max: u32) -> String {
    pick(
        lang,
        &format!("⚠️ Bạn đang chạy tối đa {} tiến trình. Đợi 1 tiến trình xong rồi gửi tiếp.", max),
        &format!("⚠️ You're already running {} processes. Wait for one to finish before sending more.", max),
    )
}

pub fn cooldown(lang: Lang, secs: u64) -> String {
    pick(
        lang,
        &format!("⏱ Chờ {}s rồi thử lại.", secs),
        &format!("⏱ Cooldown — wait {}s before retrying.", secs),
    )
}

pub fn queue_full(lang: Lang, pending: usize, capacity: usize) -> String {
    pick(
        lang,
        &format!("🚫 Hàng chờ đầy ({}/{}). Bot đang bận, thử lại sau ít phút.", pending, capacity),
        &format!("🚫 Queue full ({}/{}). Bot is busy, retry in a few minutes.", pending, capacity),
    )
}

pub fn queue_closed(lang: Lang) -> String {
    pick(lang, "🚫 Hàng chờ đã đóng.", "🚫 Queue closed.")
}

// ─── Job lifecycle ────────────────────────────────────────────────────

pub fn job_received(lang: Lang, email: &str) -> String {
    pick(
        lang,
        &format!("🚀 Đã nhận tài khoản\nEmail: {}\nĐang vào hàng chờ...", email),
        &format!("🚀 Account received\nEmail: {}\nQueueing...", email),
    )
}

pub fn qr_caption(lang: Lang, email: &str, expires: &str) -> String {
    pick(
        lang,
        &format!("✅ UPI QR\nEmail: {}\nHết hạn: {}", email, expires),
        &format!("✅ UPI QR\nEmail: {}\nExpires: {}", email, expires),
    )
}

// ─── Stop ─────────────────────────────────────────────────────────────

pub fn stopped_this(lang: Lang) -> String {
    pick(lang, "🛑 Đã dừng tiến trình này.", "🛑 This process was stopped.")
}

pub fn stop_not_found(lang: Lang) -> String {
    pick(lang, "ℹ️ Tiến trình đã kết thúc hoặc không tồn tại.", "ℹ️ Process already finished or not found.")
}

pub fn stopped_all(lang: Lang, n: usize) -> String {
    if n == 0 {
        pick(lang, "ℹ️ Không có tiến trình nào để dừng.", "ℹ️ No running processes to stop.")
    } else {
        pick(
            lang,
            &format!("🛑 Đã dừng {} tiến trình của bạn.", n),
            &format!("🛑 Stopped {} of your processes.", n),
        )
    }
}

// ─── Access ───────────────────────────────────────────────────────────

pub fn banned(lang: Lang) -> String {
    pick(lang, "⛔ Bạn đã bị admin chặn.", "⛔ You have been blocked by the admin.")
}

pub fn not_whitelisted(lang: Lang) -> String {
    pick(lang, "⛔ Tài khoản chưa được cấp quyền. Liên hệ admin.", "⛔ Account not whitelisted. Contact the admin.")
}

pub fn unknown_command(lang: Lang, cmd: &str) -> String {
    pick(
        lang,
        &format!("❓ Lệnh không hợp lệ: {}\n\nGõ /help để xem danh sách.", cmd),
        &format!("❓ Unknown command: {}\n\nType /help for the list.", cmd),
    )
}

pub fn status_online(lang: Lang) -> String {
    pick(lang, "✅ Bot đang hoạt động.", "✅ Bot online.")
}
