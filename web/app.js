"use strict";
const $ = (id) => document.getElementById(id);
let user = null,
  activeId = null,
  conversations = [],
  messages = [];
let busy = false,
  registering = false,
  pendingTurn = null;

function notice(id, text = "", success = false) {
  $(id).textContent = text;
  $(id).classList.toggle("success", success);
}

function csrfToken() {
  return (
    document.cookie
      .split("; ")
      .find((item) => item.startsWith("psych_csrf="))
      ?.slice("psych_csrf=".length) || ""
  );
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...options.headers };
  if (options.method && options.method !== "GET")
    headers["X-CSRF-Token"] = decodeURIComponent(csrfToken());
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && user) {
      showAuth();
      notice("authMessage", "登录已过期，请重新登录");
    }
    let detail =
      typeof body.detail === "string" ? body.detail : "请检查输入内容后重试";
    if (response.status === 422 && Array.isArray(body.detail)) {
      detail =
        body.detail[0]?.loc?.at(-1) === "password"
          ? "密码需为 8—128 位"
          : "请检查用户名、昵称或输入长度";
    }
    throw new Error(detail);
  }
  return body;
}

function setBusy(value) {
  busy = value;
  document
    .querySelectorAll("[data-idle], .conversation-item")
    .forEach((button) => {
      button.disabled = value;
    });
  $("input").disabled = value;
  $("send").textContent = value ? "请稍候…" : "发送";
}

async function action(statusId, work) {
  if (busy) return;
  setBusy(true);
  notice(statusId);
  try {
    await work();
  } catch (error) {
    notice(statusId, error.message);
  } finally {
    setBusy(false);
  }
}

function showAuth() {
  user = null;
  activeId = null;
  conversations = [];
  messages = [];
  pendingTurn = null;
  $("messages").replaceChildren();
  $("conversationList").replaceChildren();
  $("memoryList").replaceChildren();
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  $("appPanel").hidden = true;
  $("authPanel").hidden = false;
  $("password").value = "";
  $("profileForm").reset();
  $("passwordForm").reset();
  $("deleteAccountForm").reset();
  $("memoryForm").reset();
  $("input").value = "";
  registering = false;
  $("displayNameLabel").hidden = true;
  $("password").minLength = 1;
  $("password").autocomplete = "current-password";
  $("authSubmit").textContent = "登录";
  $("authToggle").textContent = "没有账号？创建一个";
}

function displayUser() {
  $("userLabel").textContent = `${user.display_name} · ${user.username}`;
  $("settingsButton").textContent = user.display_name;
  $("profileName").value = user.display_name;
}

async function enterApp(data) {
  user = data.user;
  $("password").value = "";
  $("authPanel").hidden = true;
  $("appPanel").hidden = false;
  displayUser();
  await loadConversations();
  const saved = localStorage.getItem(`psych_active_${user.id}`);
  const chosen =
    conversations.find((item) => item.id === saved) || conversations[0];
  if (chosen) await openConversation(chosen.id);
  else renderMessages();
}

$("authToggle").addEventListener("click", () => {
  registering = !registering;
  $("displayNameLabel").hidden = !registering;
  $("password").minLength = registering ? 8 : 1;
  $("password").autocomplete = registering
    ? "new-password"
    : "current-password";
  $("authSubmit").textContent = registering ? "创建账户" : "登录";
  $("authToggle").textContent = registering
    ? "已有账号？返回登录"
    : "没有账号？创建一个";
  notice("authMessage");
});

$("authForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("authSubmit").disabled = true;
  $("authToggle").disabled = true;
  try {
    notice("authMessage", "正在连接…", true);
    const data = await api(registering ? "/auth/register" : "/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("username").value.trim(),
        password: $("password").value,
        display_name: $("displayName").value.trim(),
      }),
    });
    await enterApp(data);
  } catch (error) {
    notice("authMessage", error.message);
  } finally {
    $("authSubmit").disabled = false;
    $("authToggle").disabled = false;
  }
});

function renderConversations() {
  $("conversationList").replaceChildren();
  for (const item of conversations) {
    const button = document.createElement("button");
    button.className =
      "conversation-item" + (item.id === activeId ? " active" : "");
    button.disabled = busy;
    const title = document.createElement("strong");
    title.textContent = item.title;
    const date = document.createElement("small");
    date.textContent = new Date(item.updated_at).toLocaleString();
    button.append(title, date);
    button.addEventListener("click", () =>
      action("chatStatus", () => openConversation(item.id)),
    );
    $("conversationList").appendChild(button);
  }
}

async function loadConversations(append = false) {
  const data = await api(
    `/conversations?offset=${append ? conversations.length : 0}`,
  );
  conversations = append
    ? conversations.concat(data.conversations)
    : data.conversations;
  $("moreConversations").hidden = !data.has_more;
  renderConversations();
}

function renderMessages() {
  $("messages").replaceChildren();
  if (!messages.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "我在。想聊聊近况，或者接着之前的话题？";
    $("messages").appendChild(empty);
  }
  for (const message of messages) {
    const row = document.createElement("div");
    row.className = `message ${message.role}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = message.content;
    const date = document.createElement("time");
    date.textContent = new Date(message.created_at).toLocaleString();
    row.append(bubble, date);
    $("messages").appendChild(row);
  }
  $("messages").scrollTop = $("messages").scrollHeight;
  $("renameConversation").hidden = !activeId;
  $("deleteConversation").hidden = !activeId;
}

async function openConversation(id) {
  const data = await api(`/conversations/${encodeURIComponent(id)}`);
  activeId = id;
  messages = data.messages;
  localStorage.setItem(`psych_active_${user.id}`, id);
  $("conversationTitle").textContent = data.title;
  $("earlierMessages").hidden = !data.has_more;
  renderConversations();
  renderMessages();
}

async function createConversation() {
  const data = await api("/conversations", { method: "POST", body: "{}" });
  await loadConversations();
  await openConversation(data.id);
}

$("newConversation").addEventListener("click", () =>
  action("chatStatus", createConversation),
);
$("moreConversations").addEventListener("click", () =>
  action("chatStatus", () => loadConversations(true)),
);
$("earlierMessages").addEventListener("click", () =>
  action("chatStatus", async () => {
    if (!activeId || !messages.length) return;
    const data = await api(
      `/conversations/${activeId}?before=${messages[0].id}`,
    );
    const oldHeight = $("messages").scrollHeight;
    messages = data.messages.concat(messages);
    renderMessages();
    $("messages").scrollTop = $("messages").scrollHeight - oldHeight;
    $("earlierMessages").hidden = !data.has_more;
  }),
);

$("renameConversation").addEventListener("click", () => {
  const title = prompt(
    "给这段对话取个名字",
    $("conversationTitle").textContent,
  );
  if (!title?.trim() || !activeId) return;
  action("chatStatus", async () => {
    await api(`/conversations/${activeId}`, {
      method: "PATCH",
      body: JSON.stringify({ title: title.trim() }),
    });
    $("conversationTitle").textContent = title.trim();
    await loadConversations();
  });
});

async function refreshAfterDelete() {
  activeId = null;
  messages = [];
  pendingTurn = null;
  localStorage.removeItem(`psych_active_${user.id}`);
  await loadConversations();
  if (conversations.length) await openConversation(conversations[0].id);
  else {
    $("conversationTitle").textContent = "想从哪里接着聊？";
    $("earlierMessages").hidden = true;
    renderMessages();
  }
}

$("deleteConversation").addEventListener("click", () => {
  if (!activeId || !confirm("删除这段对话及其聊天记录？此操作无法撤销。"))
    return;
  action("chatStatus", async () => {
    await api(`/conversations/${activeId}`, { method: "DELETE" });
    await refreshAfterDelete();
  });
});

$("chatForm").addEventListener("submit", (event) => {
  event.preventDefault();
  if (busy) return;
  const text = $("input").value.trim();
  if (!text) return;
  if (text.toLowerCase().startsWith("/show")) {
    notice("chatStatus", "咨询报告仅在管理员测试页提供。");
    return;
  }
  action("chatStatus", async () => {
    if (!activeId) await createConversation();
    if (
      !pendingTurn ||
      pendingTurn.text !== text ||
      pendingTurn.conversation_id !== activeId
    ) {
      const requestId =
        globalThis.crypto?.randomUUID?.() ||
        `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      pendingTurn = { text, conversation_id: activeId, request_id: requestId };
    }
    notice("chatStatus", "正在回复…", true);
    await api("/chat", { method: "POST", body: JSON.stringify(pendingTurn) });
    pendingTurn = null;
    $("input").value = "";
    notice("chatStatus");
    await loadConversations();
    await openConversation(activeId);
  }).then(() => $("input").focus());
});

$("input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!busy) $("chatForm").requestSubmit();
  }
});

document
  .querySelectorAll("[data-close]")
  .forEach((button) =>
    button.addEventListener("click", () => $(button.dataset.close).close()),
  );
$("settingsButton").addEventListener("click", () => {
  notice("settingsStatus");
  $("settingsDialog").showModal();
});
$("memoriesButton").addEventListener("click", () => {
  $("memoryDialog").showModal();
  action("memoryStatus", loadMemories);
});

async function loadMemories() {
  const data = await api("/memories");
  $("memoryList").replaceChildren();
  for (const item of data.memories) {
    const row = document.createElement("div");
    row.className = "memory-item";
    const text = document.createElement("textarea");
    text.value = item.content;
    text.maxLength = 500;
    text.setAttribute("aria-label", "编辑记忆");
    const buttons = document.createElement("div");
    buttons.className = "actions";
    const save = document.createElement("button");
    save.textContent = "更新";
    save.dataset.idle = "";
    save.addEventListener("click", () =>
      action("memoryStatus", async () => {
        await api(`/memories/${item.id}`, {
          method: "PATCH",
          body: JSON.stringify({ content: text.value.trim() }),
        });
        await loadMemories();
        notice("memoryStatus", "已更新", true);
      }),
    );
    const remove = document.createElement("button");
    remove.textContent = "删除";
    remove.className = "quiet danger-text";
    remove.dataset.idle = "";
    remove.addEventListener("click", () => {
      if (!confirm("删除这条记忆？对应聊天原文仍会保留。")) return;
      action("memoryStatus", async () => {
        await api(`/memories/${item.id}`, { method: "DELETE" });
        await loadMemories();
      });
    });
    buttons.append(save, remove);
    row.append(text, buttons);
    $("memoryList").appendChild(row);
  }
}

$("memoryForm").addEventListener("submit", (event) => {
  event.preventDefault();
  action("memoryStatus", async () => {
    await api("/memories", {
      method: "POST",
      body: JSON.stringify({ content: $("memoryContent").value.trim() }),
    });
    $("memoryContent").value = "";
    await loadMemories();
    notice("memoryStatus", "已保存", true);
  });
});

$("profileForm").addEventListener("submit", (event) => {
  event.preventDefault();
  action("settingsStatus", async () => {
    const data = await api("/account", {
      method: "PATCH",
      body: JSON.stringify({ display_name: $("profileName").value.trim() }),
    });
    user = data.user;
    displayUser();
    notice("settingsStatus", "昵称已保存", true);
  });
});

$("passwordForm").addEventListener("submit", (event) => {
  event.preventDefault();
  action("settingsStatus", async () => {
    await api("/account/password", {
      method: "POST",
      body: JSON.stringify({
        current_password: $("currentPassword").value,
        new_password: $("newPassword").value,
      }),
    });
    $("passwordForm").reset();
    notice("settingsStatus", "密码已修改，其他设备已退出登录", true);
  });
});

$("logoutButton").addEventListener("click", () =>
  action("chatStatus", async () => {
    await api("/auth/logout", { method: "POST" });
    showAuth();
    notice("authMessage", "已退出登录", true);
  }),
);

$("exportButton").addEventListener("click", () =>
  action("settingsStatus", async () => {
    const data = await api("/account/export");
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = "我的聊天记录.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notice("settingsStatus", "导出文件已生成，请妥善保存", true);
  }),
);

$("clearHistory").addEventListener("click", () => {
  if (!confirm("删除全部对话？“记住的事”会保留。此操作无法撤销。")) return;
  action("settingsStatus", async () => {
    await api("/history", { method: "DELETE" });
    await refreshAfterDelete();
    notice("settingsStatus", "对话记录已删除", true);
  });
});

$("deleteAccountForm").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!confirm("确定注销？账户、登录凭据、全部对话和记忆都会永久删除。"))
    return;
  action("settingsStatus", async () => {
    await api("/account", {
      method: "DELETE",
      body: JSON.stringify({ password: $("deletePassword").value }),
    });
    localStorage.removeItem(`psych_active_${user.id}`);
    showAuth();
    notice("authMessage", "账户及数据已删除", true);
  });
});

(async () => {
  const [health, login] = await Promise.allSettled([
    api("/health"),
    api("/auth/me"),
  ]);
  if (health.status === "fulfilled")
    $("serviceNotice").hidden = health.value.model_configured;
  if (login.status === "fulfilled") {
    try {
      await enterApp(login.value);
    } catch (error) {
      notice("chatStatus", error.message);
    }
  } else if (health.status === "rejected")
    notice("authMessage", "暂时无法连接服务，请稍后刷新重试");
})();
