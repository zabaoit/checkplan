//! Live job board — snapshot trung tâm của mọi job đang trong hệ thống, phục
//! vụ bảng trạng thái realtime cho admin (`/board`).
//!
//! Khác `JobRegistry` (chỉ giữ cancel token để `/stop`), board giữ thêm trạng
//! thái hiển thị: user, email (đã mask), state, step (log cuối), mốc thời gian.
//!
//! Vòng đời 1 entry (key = `job_id` u64 từ `JobRegistry::register`):
//!   1. submit thành công  → `insert_queued`
//!   2. worker pickup       → `mark_running`
//!   3. mỗi log line        → `set_step`
//!   4. Done/Timeout/Cancel → `remove`
//!
//! Map sync + ngắn, không giữ guard qua `.await` (tokio `Mutex` vẫn dùng để
//! await an toàn nếu cần). Render là pure function trên snapshot đã clone.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use tokio::sync::Mutex;

/// Trạng thái 1 job trong board.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobState {
    Queued,
    Running,
}

impl JobState {
    fn icon(self) -> &'static str {
        match self {
            JobState::Queued => "⏳",
            JobState::Running => "▶️",
        }
    }
    fn label(self) -> &'static str {
        match self {
            JobState::Queued => "queue",
            JobState::Running => "run",
        }
    }
}

#[derive(Debug, Clone)]
pub struct JobStatus {
    pub user_id: i64,
    pub username: Option<String>,
    /// Email đã mask sẵn (không lưu plaintext).
    pub email_masked: String,
    pub state: JobState,
    /// Log line gần nhất — hiển thị cột "step". Rỗng khi chưa có log.
    pub step: String,
    /// Mốc tính tuổi: thời điểm submit (Queued) hoặc thời điểm chạy (Running).
    pub since: Instant,
}

#[derive(Clone)]
pub struct JobBoard {
    inner: Arc<Mutex<HashMap<u64, JobStatus>>>,
}

impl JobBoard {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Thêm job vừa submit vào board (state = Queued).
    pub async fn insert_queued(
        &self,
        job_id: u64,
        user_id: i64,
        username: Option<String>,
        email_masked: String,
    ) {
        let mut g = self.inner.lock().await;
        g.insert(
            job_id,
            JobStatus {
                user_id,
                username,
                email_masked,
                state: JobState::Queued,
                step: String::new(),
                since: Instant::now(),
            },
        );
    }

    /// Worker pickup → chuyển sang Running, reset mốc tuổi về thời điểm chạy.
    pub async fn mark_running(&self, job_id: u64) {
        let mut g = self.inner.lock().await;
        if let Some(s) = g.get_mut(&job_id) {
            s.state = JobState::Running;
            s.since = Instant::now();
        }
    }

    /// Cập nhật log line gần nhất cho job.
    pub async fn set_step(&self, job_id: u64, step: String) {
        let mut g = self.inner.lock().await;
        if let Some(s) = g.get_mut(&job_id) {
            s.step = step;
        }
    }

    /// Job kết thúc (Done/Timeout/Cancelled) → bỏ khỏi board.
    pub async fn remove(&self, job_id: u64) {
        self.inner.lock().await.remove(&job_id);
    }

    /// Snapshot sắp xếp ổn định: Running trước Queued, rồi theo tuổi giảm dần
    /// (job lâu nhất lên đầu). Clone để render không giữ lock.
    pub async fn snapshot(&self) -> Vec<JobStatus> {
        let g = self.inner.lock().await;
        let mut out: Vec<JobStatus> = g.values().cloned().collect();
        out.sort_by(|a, b| match (a.state, b.state) {
            (JobState::Running, JobState::Queued) => std::cmp::Ordering::Less,
            (JobState::Queued, JobState::Running) => std::cmp::Ordering::Greater,
            _ => b.since.cmp(&a.since).reverse(),
        });
        out
    }
}

/// Escape ký tự đặc biệt HTML cho rich message (tránh vỡ định dạng / API từ chối).
fn html_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            _ => out.push(c),
        }
    }
    out
}

/// Cắt chuỗi theo số ký tự (char-safe), thêm '…' nếu bị cắt.
fn truncate_chars(s: &str, max: usize) -> String {
    let trimmed = s.trim();
    if trimmed.chars().count() <= max {
        return trimmed.to_string();
    }
    let mut out: String = trimmed.chars().take(max.saturating_sub(1)).collect();
    out.push('…');
    out
}

/// Định dạng tuổi job dạng mm:ss (hoặc h:mm:ss khi >= 1 giờ).
fn fmt_age(secs: u64) -> String {
    let h = secs / 3600;
    let m = (secs % 3600) / 60;
    let s = secs % 60;
    if h > 0 {
        format!("{}:{:02}:{:02}", h, m, s)
    } else {
        format!("{:02}:{:02}", m, s)
    }
}

/// Render snapshot thành rich-message HTML (heading + table). `now` truyền vào
/// để tính tuổi nhất quán trong 1 lần render. `clock` = nhãn giờ hiển thị.
pub fn render_board_html(snapshot: &[JobStatus], now: Instant, clock: &str) -> String {
    let running = snapshot
        .iter()
        .filter(|s| s.state == JobState::Running)
        .count();
    let queued = snapshot.len() - running;

    let mut html = String::with_capacity(256 + snapshot.len() * 160);
    html.push_str(&format!(
        "<h3>📊 Processes đang chạy</h3><p>▶️ {} run · ⏳ {} queue · 🕒 {}</p>",
        running,
        queued,
        html_escape(clock),
    ));

    if snapshot.is_empty() {
        html.push_str("<p><i>Không có process nào đang chạy.</i></p>");
        return html;
    }

    html.push_str(
        "<table bordered striped>\
         <tr><th>#</th><th>User</th><th>Email</th><th>State</th><th>Step</th><th>Age</th></tr>",
    );

    for (i, s) in snapshot.iter().enumerate() {
        let user_cell = match s.username.as_deref() {
            Some(u) if !u.is_empty() => format!("@{}", html_escape(u)),
            _ => format!("id{}", s.user_id),
        };
        let step_cell = if s.step.is_empty() {
            "—".to_string()
        } else {
            html_escape(&truncate_chars(&s.step, 40))
        };
        let age = fmt_age(now.saturating_duration_since(s.since).as_secs());

        html.push_str(&format!(
            "<tr><td>{}</td><td>{}</td><td><code>{}</code></td><td>{} {}</td><td>{}</td><td>{}</td></tr>",
            i + 1,
            user_cell,
            html_escape(&s.email_masked),
            s.state.icon(),
            s.state.label(),
            step_cell,
            age,
        ));
    }

    html.push_str("</table>");
    html
}

/// Cờ chống chạy nhiều task refresh `/board` cùng lúc. `/board` chỉ spawn task
/// mới khi đang inactive; task tự `deactivate` khi thoát (board rỗng quá lâu).
#[derive(Clone)]
pub struct LiveBoardCtl {
    active: Arc<std::sync::atomic::AtomicBool>,
}

impl LiveBoardCtl {
    pub fn new() -> Self {
        Self {
            active: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        }
    }

    /// Thử bật. Trả `true` nếu vừa chuyển từ inactive → active (được phép
    /// spawn task). `false` nếu đã có task đang chạy.
    pub fn try_activate(&self) -> bool {
        self.active
            .compare_exchange(
                false,
                true,
                std::sync::atomic::Ordering::SeqCst,
                std::sync::atomic::Ordering::SeqCst,
            )
            .is_ok()
    }

    pub fn deactivate(&self) {
        self.active.store(false, std::sync::atomic::Ordering::SeqCst);
    }
}
