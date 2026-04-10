let activeMeetingId = null;
let uploadedMeetings = [];
let chatHistory = [];
let enhancementPollTimer = null;
let baseUploadStatusText = "";
let geminiCooldownTimer = null;
let geminiCooldownRemaining = 0;
let geminiRetryingNowTimeout = null;
const ALL_MEETINGS_VALUE = "__ALL_MEETINGS__";

const fileInput = document.getElementById("fileInput");
const projectInput = document.getElementById("projectInput");
const meetingDateInput = document.getElementById("meetingDateInput");
const uploadBtn = document.getElementById("uploadBtn");
const uploadStatus = document.getElementById("uploadStatus");
const metadataBox = document.getElementById("metadataBox");
const decisionsList = document.getElementById("decisionsList");
const actionsTableBody = document.getElementById("actionsTableBody");
const sentimentTimeline = document.getElementById("sentimentTimeline");
const speakerSummaryBody = document.getElementById("speakerSummaryBody");
const questionInput = document.getElementById("questionInput");
const askBtn = document.getElementById("askBtn");
const meetingSelect = document.getElementById("meetingSelect");
const chatMode = document.getElementById("chatMode");
const answerBox = document.getElementById("answerBox");
const sourcesList = document.getElementById("sourcesList");
const geminiBadge = document.getElementById("geminiBadge");

function stopGeminiCooldownTicker() {
  if (geminiCooldownTimer) {
    clearInterval(geminiCooldownTimer);
    geminiCooldownTimer = null;
  }
}

function clearGeminiRetryingNowTimeout() {
  if (geminiRetryingNowTimeout) {
    clearTimeout(geminiRetryingNowTimeout);
    geminiRetryingNowTimeout = null;
  }
}

function showGeminiRetryingNowState() {
  if (!geminiBadge) {
    return;
  }

  clearGeminiRetryingNowTimeout();
  geminiBadge.textContent = "Gemini retrying now...";
  geminiBadge.hidden = false;

  geminiRetryingNowTimeout = setTimeout(() => {
    geminiBadge.hidden = true;
    geminiBadge.textContent = "";
    geminiRetryingNowTimeout = null;
  }, 1800);
}

function renderGeminiCooldownBadge() {
  if (!geminiBadge) {
    return;
  }

  const secondsLabel = geminiCooldownRemaining > 0 ? ` (${geminiCooldownRemaining}s)` : "";
  geminiBadge.textContent = `Gemini temporarily paused (quota cooldown)${secondsLabel}`;
  geminiBadge.hidden = false;
}

function startGeminiCooldownTicker() {
  stopGeminiCooldownTicker();
  clearGeminiRetryingNowTimeout();

  if (geminiCooldownRemaining <= 0) {
    return;
  }

  geminiCooldownTimer = setInterval(() => {
    geminiCooldownRemaining = Math.max(0, geminiCooldownRemaining - 1);
    if (geminiCooldownRemaining <= 0) {
      stopGeminiCooldownTicker();
      showGeminiRetryingNowState();
      return;
    }
    renderGeminiCooldownBadge();
  }, 1000);
}

function stopEnhancementPolling() {
  if (enhancementPollTimer) {
    clearInterval(enhancementPollTimer);
    enhancementPollTimer = null;
  }
}

function enhancementStateLabel(item) {
  const status = String((item && item.enhancement_status) || "").toLowerCase();
  if (status === "completed") {
    return "enhanced";
  }
  if (status === "failed") {
    return "draft (Gemini unavailable)";
  }
  if (status === "processing" || status === "queued") {
    return "draft (enhancing...)";
  }
  return "draft";
}

function updateEnhancementStatusLine() {
  if (!uploadedMeetings.length) {
    return;
  }

  const done = uploadedMeetings.filter((item) => item.enhancement_status === "completed").length;
  const failed = uploadedMeetings.filter((item) => item.enhancement_status === "failed").length;
  const pending = uploadedMeetings.length - done - failed;

  const suffix = `Enhancement: ${done}/${uploadedMeetings.length} enhanced` +
    (pending ? `, ${pending} processing` : "") +
    (failed ? `, ${failed} fallback` : "");

  uploadStatus.textContent = baseUploadStatusText
    ? `${baseUploadStatusText} | ${suffix}`
    : suffix;
}

function rerenderCurrentContext() {
  if (!uploadedMeetings.length) {
    return;
  }

  if (!activeMeetingId) {
    const mergedInsights = buildMergedInsights(uploadedMeetings);
    const mergedSentiment = buildMergedSentiment(uploadedMeetings);
    metadataBox.textContent = JSON.stringify(
      {
        context: "All uploaded meetings",
        meeting_count: uploadedMeetings.length,
        merged_decision_count: mergedInsights.decisions.length,
        merged_action_item_count: mergedInsights.action_items.length,
        merged_timeline_entries: mergedSentiment.timeline.length,
      },
      null,
      2,
    );
    renderInsights(mergedInsights);
    renderSentiment(mergedSentiment);
    return;
  }

  const selectedMeeting = uploadedMeetings.find((item) => item.meeting_id === activeMeetingId);
  if (!selectedMeeting) {
    return;
  }

  metadataBox.textContent = JSON.stringify(
    {
      ...(selectedMeeting.metadata || {}),
      insight_state: enhancementStateLabel(selectedMeeting),
    },
    null,
    2,
  );
  renderInsights(selectedMeeting.insights || {});
  renderSentiment(selectedMeeting.sentiment || {});
}

async function pollEnhancementStatus() {
  if (!uploadedMeetings.length) {
    stopEnhancementPolling();
    return;
  }

  const ids = uploadedMeetings.map((item) => item.meeting_id).filter(Boolean);
  if (!ids.length) {
    stopEnhancementPolling();
    return;
  }

  try {
    const response = await fetch(`/api/insights_status?meeting_ids=${encodeURIComponent(ids.join(","))}`);
    if (!response.ok) {
      return;
    }

    const data = await response.json();
    const updates = data.updates || {};
    let changed = false;

    uploadedMeetings = uploadedMeetings.map((meeting) => {
      const update = updates[meeting.meeting_id];
      if (!update) {
        return meeting;
      }

      const next = { ...meeting };
      if (update.insights) {
        next.insights = update.insights;
      }
      if (update.enhancement_status) {
        next.enhancement_status = update.enhancement_status;
      }

      if (
        next.insights !== meeting.insights ||
        next.enhancement_status !== meeting.enhancement_status
      ) {
        changed = true;
      }

      return next;
    });

    if (changed) {
      rerenderCurrentContext();
    }

    updateEnhancementStatusLine();

    const hasPending = uploadedMeetings.some(
      (item) => item.enhancement_status === "queued" || item.enhancement_status === "processing",
    );
    if (!hasPending) {
      stopEnhancementPolling();
    }
  } catch {
    // Keep current UI state and retry on next interval tick.
  }
}

function startEnhancementPolling() {
  stopEnhancementPolling();
  enhancementPollTimer = setInterval(pollEnhancementStatus, 1500);
  pollEnhancementStatus();
}

function populateMeetingSelector(uploads) {
  meetingSelect.innerHTML = "";

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Select a meeting";
  meetingSelect.appendChild(placeholder);

  if (uploads.length > 1) {
    const allOption = document.createElement("option");
    allOption.value = ALL_MEETINGS_VALUE;
    allOption.textContent = "All uploaded meetings";
    meetingSelect.appendChild(allOption);
  }

  uploads.forEach((item, index) => {
    const option = document.createElement("option");
    option.value = item.meeting_id;
    option.textContent = `${index + 1}. ${item.filename} (${item.meeting_date})`;
    meetingSelect.appendChild(option);
  });
}

meetingSelect.addEventListener("change", () => {
  const selectedId = (meetingSelect.value || "").trim();
  if (!selectedId) {
    return;
  }

  if (selectedId === ALL_MEETINGS_VALUE) {
    activeMeetingId = null;
    chatHistory = [];
    setGeminiCooldownBadge(false, 0);
    rerenderCurrentContext();
    answerBox.textContent = "Ready for cross-meeting questions.";
    chatMode.textContent = "";
    sourcesList.innerHTML = "";
    return;
  }

  const selectedMeeting = uploadedMeetings.find((item) => item.meeting_id === selectedId);
  if (!selectedMeeting) {
    return;
  }

  activeMeetingId = selectedId;
  chatHistory = [];
  setGeminiCooldownBadge(false, 0);
  rerenderCurrentContext();
  answerBox.textContent = `Ready for questions on ${selectedMeeting.filename}.`;
  chatMode.textContent = "";
  sourcesList.innerHTML = "";
});

function renderInsights(insights) {
  decisionsList.innerHTML = "";
  actionsTableBody.innerHTML = "";

  const decisions = insights.decisions || [];
  const actionItems = insights.action_items || [];

  if (!decisions.length) {
    const li = document.createElement("li");
    li.textContent = "No explicit decisions found.";
    decisionsList.appendChild(li);
  } else {
    decisions.forEach((decision) => {
      const li = document.createElement("li");
      li.textContent = decision;
      decisionsList.appendChild(li);
    });
  }

  if (!actionItems.length) {
    const row = document.createElement("tr");
    row.innerHTML = "<td colspan=\"3\">No action items found.</td>";
    actionsTableBody.appendChild(row);
  } else {
    actionItems.forEach((item) => {
      const row = document.createElement("tr");
      row.innerHTML = `<td>${item.person}</td><td>${item.task}</td><td>${item.deadline}</td>`;
      actionsTableBody.appendChild(row);
    });
  }
}

function renderSources(citations) {
  sourcesList.innerHTML = "";

  if (!citations || !citations.length) {
    const li = document.createElement("li");
    li.textContent = "No citations available.";
    sourcesList.appendChild(li);
    return;
  }

  citations.forEach((citation) => {
    const li = document.createElement("li");
    const score = Number(citation.score).toFixed(3);
    li.textContent = `[${citation.rank}] (score: ${score}) ${citation.text}`;
    sourcesList.appendChild(li);
  });
}

function sentimentClass(sentiment) {
  if (sentiment === "agreement") {
    return "chip-agreement";
  }
  if (sentiment === "conflict") {
    return "chip-conflict";
  }
  if (sentiment === "frustration") {
    return "chip-frustration";
  }
  return "chip-neutral";
}

function renderSentiment(sentiment) {
  sentimentTimeline.innerHTML = "";
  speakerSummaryBody.innerHTML = "";

  const timeline = (sentiment && sentiment.timeline) || [];
  const speakerSummary = (sentiment && sentiment.speaker_summary) || [];

  if (!timeline.length) {
    const li = document.createElement("li");
    li.textContent = "No sentiment timeline available.";
    sentimentTimeline.appendChild(li);
  } else {
    timeline.forEach((entry) => {
      const li = document.createElement("li");
      li.className = "timeline-item";
      li.innerHTML = `<span class="sentiment-chip ${sentimentClass(entry.sentiment)}">${entry.sentiment}</span> <strong>${entry.speaker}</strong>: ${entry.text}`;
      sentimentTimeline.appendChild(li);
    });
  }

  if (!speakerSummary.length) {
    const row = document.createElement("tr");
    row.innerHTML = "<td colspan=\"5\">No speaker summary available.</td>";
    speakerSummaryBody.appendChild(row);
  } else {
    speakerSummary.forEach((item) => {
      const row = document.createElement("tr");
      row.innerHTML = `<td>${item.speaker}</td><td>${item.agreement}</td><td>${item.conflict}</td><td>${item.frustration}</td><td>${item.neutral}</td>`;
      speakerSummaryBody.appendChild(row);
    });
  }
}

function formatGenerationIssue(rawError) {
  const message = String(rawError || "").trim();
  if (!message) {
    return "";
  }

  const lower = message.toLowerCase();
  if (lower.includes("resource_exhausted") || lower.includes("quota exceeded") || lower.includes("429")) {
    return "Gemini quota exceeded. Using fallback answer.";
  }

  if (lower.includes("unavailable") || lower.includes("503")) {
    return "Gemini temporarily unavailable. Using fallback answer.";
  }

  if (message.length > 120) {
    return `${message.slice(0, 117)}...`;
  }

  return message;
}

function setGeminiCooldownBadge(isActive, remainingSeconds) {
  if (!geminiBadge) {
    return;
  }

  if (!isActive) {
    geminiCooldownRemaining = 0;
    stopGeminiCooldownTicker();
    clearGeminiRetryingNowTimeout();
    geminiBadge.hidden = true;
    geminiBadge.textContent = "";
    return;
  }

  clearGeminiRetryingNowTimeout();
  const remaining = Number(remainingSeconds || 0);
  geminiCooldownRemaining = Number.isFinite(remaining) && remaining > 0 ? Math.floor(remaining) : 0;
  renderGeminiCooldownBadge();
  startGeminiCooldownTicker();
}

function buildMergedInsights(meetingsList) {
  const decisionSet = new Set();
  const actionMap = new Map();

  meetingsList.forEach((meeting) => {
    const insights = meeting.insights || {};
    const decisions = insights.decisions || [];
    const actionItems = insights.action_items || [];

    decisions.forEach((decision) => {
      const normalized = String(decision || "").trim();
      if (normalized) {
        decisionSet.add(normalized);
      }
    });

    actionItems.forEach((item) => {
      const person = String(item.person || "").trim() || "Unassigned";
      const task = String(item.task || "").trim();
      const deadline = String(item.deadline || "").trim() || "Not specified";
      if (!task) {
        return;
      }

      const key = `${person.toLowerCase()}|${task.toLowerCase()}|${deadline.toLowerCase()}`;
      if (!actionMap.has(key)) {
        actionMap.set(key, { person, task, deadline });
      }
    });
  });

  return {
    decisions: Array.from(decisionSet),
    action_items: Array.from(actionMap.values()),
  };
}

function buildMergedSentiment(meetingsList) {
  const timeline = [];
  const speakerSummaryMap = new Map();

  meetingsList.forEach((meeting) => {
    const sentiment = meeting.sentiment || {};
    const meetingLabel = meeting.filename || "Meeting";

    (sentiment.timeline || []).forEach((entry) => {
      timeline.push({
        speaker: entry.speaker,
        sentiment: entry.sentiment,
        text: `[${meetingLabel}] ${entry.text}`,
      });
    });

    (sentiment.speaker_summary || []).forEach((item) => {
      const existing = speakerSummaryMap.get(item.speaker) || {
        speaker: item.speaker,
        agreement: 0,
        conflict: 0,
        frustration: 0,
        neutral: 0,
      };

      existing.agreement += Number(item.agreement || 0);
      existing.conflict += Number(item.conflict || 0);
      existing.frustration += Number(item.frustration || 0);
      existing.neutral += Number(item.neutral || 0);
      speakerSummaryMap.set(item.speaker, existing);
    });
  });

  return {
    timeline,
    speaker_summary: Array.from(speakerSummaryMap.values()).sort((a, b) =>
      a.speaker.localeCompare(b.speaker),
    ),
  };
}

uploadBtn.addEventListener("click", async () => {
  const files = Array.from(fileInput.files || []);
  if (!files.length) {
    uploadStatus.textContent = "Choose one or more .txt/.vtt files first.";
    return;
  }

  uploadStatus.textContent = "Uploading and processing transcript...";
  setGeminiCooldownBadge(false, 0);
  metadataBox.textContent = "";

  const project = (projectInput.value || "").trim() || "General";
  const meetingDate = (meetingDateInput.value || "").trim();

  const formData = new FormData();
  files.forEach((file) => {
    formData.append("files", file);
  });
  formData.append("project", project);
  if (meetingDate) {
    formData.append("meeting_date", meetingDate);
  }

  try {
    const response = await fetch("/api/upload", {
      method: "POST",
      body: formData,
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Upload failed");
    }

    const uploads = data.uploads || [];
    uploadedMeetings = uploads.length ? uploads : [data];
    const firstUpload = uploadedMeetings[0];
    activeMeetingId = firstUpload.meeting_id;
    populateMeetingSelector(uploadedMeetings);
    if (uploadedMeetings.length > 1) {
      meetingSelect.value = ALL_MEETINGS_VALUE;
      activeMeetingId = null;
      chatHistory = [];
      rerenderCurrentContext();
      answerBox.textContent = "Ready for cross-meeting questions.";
      sourcesList.innerHTML = "";
    } else {
      meetingSelect.value = activeMeetingId;
      chatHistory = [];
      rerenderCurrentContext();
      answerBox.textContent = "Ready for questions.";
      sourcesList.innerHTML = "";
    }

    const uploadedNames = uploadedMeetings.length
      ? uploadedMeetings.map((item) => item.filename).join(", ")
      : firstUpload.metadata.filename;
    const contextText = activeMeetingId
      ? `Context: ${activeMeetingId}`
      : "Context: All uploaded meetings";
    baseUploadStatusText = `Uploaded ${uploadedMeetings.length} file(s): ${uploadedNames} | Project: ${data.project} | Date: ${data.meeting_date} | ${contextText}`;
    updateEnhancementStatusLine();
    startEnhancementPolling();
  } catch (error) {
    stopEnhancementPolling();
    uploadStatus.textContent = `Error: ${error.message}`;
  }
});

askBtn.addEventListener("click", async () => {
  const question = questionInput.value.trim();
  if (!question) {
    answerBox.textContent = "Type a question first.";
    return;
  }

  if (!uploadedMeetings.length) {
    answerBox.textContent = "Upload a transcript before asking questions.";
    return;
  }

  chatMode.textContent = "Thinking...";

  try {
    const compactHistory = chatHistory.slice(-6).map((item) => ({
      role: item.role,
      content: String(item.content || "").slice(0, 600),
    }));

    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(
        activeMeetingId
          ? {
              question,
              meeting_id: activeMeetingId,
              history: compactHistory,
            }
          : {
              question,
              history: compactHistory,
            },
      ),
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Chat request failed");
    }

    const generationMode = data.generation_mode || "unknown";
    const retrievalMode = data.retrieval_mode || "unknown";
    const generationModel = data.generation_model || "n/a";
    const generationError = data.generation_error || "";
    const quotaCooldownActive = Boolean(data.gemini_quota_cooldown_active);
    const quotaCooldownRemaining = Number(data.gemini_quota_cooldown_remaining_seconds || 0);
    chatMode.textContent = `Generation: ${generationMode} (${generationModel}) | Retrieval: ${retrievalMode}`;
    if (generationMode === "fallback" && generationError) {
      chatMode.textContent += ` | ${formatGenerationIssue(generationError)}`;
    }
    setGeminiCooldownBadge(quotaCooldownActive, quotaCooldownRemaining);
    answerBox.textContent = data.answer || "No answer returned.";
    renderSources(data.citations || []);

    chatHistory.push({ role: "user", content: question });
    chatHistory.push({ role: "assistant", content: data.answer || "" });
    if (chatHistory.length > 12) {
      chatHistory = chatHistory.slice(-12);
    }
  } catch (error) {
    setGeminiCooldownBadge(false, 0);
    chatMode.textContent = "";
    answerBox.textContent = `Error: ${error.message}`;
  }
});
