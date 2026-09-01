"use strict";

const input = document.querySelector("#search-input");
const ghostInput = document.querySelector("#search-ghost");
const clearButton = document.querySelector("#clear-button");
const voiceButton = document.querySelector("#voice-button");
const loadingIndicator = document.querySelector("#loading-indicator");
const searchShell = document.querySelector("#search-shell");
const suggestionList = document.querySelector("#suggestions");
const searchStatus = document.querySelector("#search-status");
const adaptationNotice = document.querySelector("#adaptation-notice");
const selectionCard = document.querySelector("#selection-card");
const selectedSentence = document.querySelector("#selected-sentence");
const selectedSource = document.querySelector("#selected-source");

let suggestions = [];
let activeIndex = -1;
let debounceTimer = null;
let currentRequest = null;
let nextWordRequest = null;
let speechRecognition = null;
let isListening = false;
let voiceRecognitionFailed = false;
let lastInputMethod = "typed";

const BrowserSpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

function recordClientEvent(eventType, details = {}) {
  fetch("/api/events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_type: eventType, details }),
    keepalive: true,
  }).catch(() => {});
}

function setOpen(isOpen) {
  suggestionList.hidden = !isOpen;
  searchShell.classList.toggle("search-shell--open", isOpen);
  input.setAttribute("aria-expanded", String(isOpen));
  if (!isOpen) {
    input.setAttribute("aria-activedescendant", "");
  }
}

function updateActiveSuggestion() {
  const elements = suggestionList.querySelectorAll(".suggestion");
  elements.forEach((element, index) => {
    const isActive = index === activeIndex;
    element.classList.toggle("suggestion--active", isActive);
    element.setAttribute("aria-selected", String(isActive));
  });
  input.setAttribute(
    "aria-activedescendant",
    activeIndex >= 0 ? `suggestion-${activeIndex}` : "",
  );
}

function suggestionElement(suggestion, index) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "suggestion";
  button.id = `suggestion-${index}`;
  button.setAttribute("role", "option");
  button.setAttribute("aria-selected", "false");

  const rank = document.createElement("span");
  rank.className = "suggestion__rank";
  rank.textContent = String(index + 1);

  const content = document.createElement("span");
  content.className = "suggestion__content";

  const sentence = document.createElement("span");
  sentence.className = "suggestion__sentence";
  sentence.textContent = suggestion.completed_sentence;

  const source = document.createElement("span");
  source.className = "suggestion__source";
  source.textContent = `${suggestion.source_text}:${suggestion.offset}`;

  const score = document.createElement("span");
  score.className = "suggestion__score";
  score.textContent = `Score ${suggestion.score}`;

  content.append(sentence, source);
  button.append(rank, content, score);
  button.addEventListener("mousedown", (event) => event.preventDefault());
  button.addEventListener("click", () => selectSuggestion(index));
  button.addEventListener("mouseenter", () => {
    activeIndex = index;
    updateActiveSuggestion();
  });
  return button;
}

function renderSuggestions(items) {
  suggestions = items;
  activeIndex = -1;
  suggestionList.replaceChildren(
    ...items.map((suggestion, index) => suggestionElement(suggestion, index)),
  );
  setOpen(items.length > 0 && document.activeElement === input);
  searchStatus.textContent = items.length > 0
    ? `${items.length} suggestion${items.length === 1 ? "" : "s"} found`
    : "No matching sentences found";
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function updateAdaptationNotice(payload) {
  if (!adaptationNotice) return;
  if (!payload || (!payload.was_adapted && !payload.warning)) {
    adaptationNotice.hidden = true;
    adaptationNotice.replaceChildren();
    return;
  }

  adaptationNotice.replaceChildren();
  adaptationNotice.hidden = false;

  if (payload.warning) {
    const warningEl = document.createElement("div");
    warningEl.className = "adaptation-notice__warning";
    warningEl.textContent = `⚠️ ${payload.warning}`;
    adaptationNotice.appendChild(warningEl);
  }

  if (payload.was_adapted) {
    const remapEl = document.createElement("div");
    remapEl.className = "adaptation-notice__remap";
    remapEl.innerHTML = `Showing results for <strong>${escapeHtml(payload.adapted_query)}</strong> <span class="adaptation-notice__original">(remapped from "${escapeHtml(payload.original_query)}")</span>`;
    adaptationNotice.appendChild(remapEl);
  }
}

async function fetchSuggestions(query, inputMethod = "typed") {
  if (currentRequest) {
    currentRequest.abort();
  }
  currentRequest = new AbortController();
  loadingIndicator.hidden = false;

  try {
    const response = await fetch(
      `/api/suggestions?query=${encodeURIComponent(query)}&input_method=${encodeURIComponent(inputMethod)}`,
      { signal: currentRequest.signal },
    );
    if (!response.ok) {
      throw new Error("Search request failed");
    }
    const payload = await response.json();
    if (input.value === query) {
      lastInputMethod = inputMethod;
      renderSuggestions(payload.suggestions);
      updateAdaptationNotice(payload);
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      suggestions = [];
      suggestionList.replaceChildren();
      setOpen(false);
      searchStatus.textContent = "Search is temporarily unavailable";
      if (adaptationNotice) {
        adaptationNotice.hidden = true;
        adaptationNotice.replaceChildren();
      }
    }
  } finally {
    if (input.value === query) {
      loadingIndicator.hidden = true;
    }
  }
}

async function fetchNextWord(query) {
  if (nextWordRequest) {
    nextWordRequest.abort();
  }
  nextWordRequest = new AbortController();

  try {
    const response = await fetch(
      `/api/next_word?query=${encodeURIComponent(query)}`,
      { signal: nextWordRequest.signal },
    );
    if (!response.ok) {
      throw new Error("Next-word request failed");
    }
    const payload = await response.json();
    if (input.value === query) {
      ghostInput.value = payload.next_word ? `${query}${payload.next_word}` : "";
      ghostInput.scrollLeft = input.scrollLeft;
    }
  } catch (error) {
    if (error.name !== "AbortError" && input.value === query) {
      ghostInput.value = "";
    }
  }
}

function scheduleSearch() {
  const query = input.value;
  lastInputMethod = "typed";
  clearButton.hidden = query.length === 0;
  selectionCard.hidden = true;
  clearTimeout(debounceTimer);
  ghostInput.value = "";
  if (nextWordRequest) {
    nextWordRequest.abort();
  }

  if (query.trim().length === 0) {
    if (currentRequest) {
      currentRequest.abort();
    }
    loadingIndicator.hidden = true;
    suggestions = [];
    suggestionList.replaceChildren();
    searchStatus.textContent = "";
    if (adaptationNotice) {
      adaptationNotice.hidden = true;
      adaptationNotice.replaceChildren();
    }
    setOpen(false);
    return;
  }

  debounceTimer = window.setTimeout(() => {
    fetchSuggestions(query, "typed");
    fetchNextWord(query);
  }, 120);
}

function setListening(listening) {
  isListening = listening;
  voiceButton.classList.toggle("voice-button--listening", listening);
  voiceButton.setAttribute("aria-pressed", String(listening));
  voiceButton.setAttribute(
    "aria-label",
    listening ? "Stop voice search" : "Search by voice",
  );
  voiceButton.title = listening ? "Stop listening" : "Search by voice";
}

function speechErrorMessage(errorCode) {
  if (errorCode === "not-allowed" || errorCode === "service-not-allowed") {
    return "Microphone access was denied. Allow microphone access and try again.";
  }
  if (errorCode === "audio-capture") {
    return "No working microphone was found.";
  }
  if (errorCode === "no-speech") {
    return "No speech was detected. Select the microphone and try again.";
  }
  if (errorCode === "network") {
    return "Voice recognition needs a network connection.";
  }
  return "Voice recognition could not complete. Please try again.";
}

function configureVoiceSearch() {
  if (!BrowserSpeechRecognition) {
    voiceButton.disabled = true;
    voiceButton.title = "Voice search is not supported by this browser";
    voiceButton.setAttribute(
      "aria-label",
      "Voice search is not supported by this browser",
    );
    return;
  }

  speechRecognition = new BrowserSpeechRecognition();
  speechRecognition.lang = "en-US";
  speechRecognition.continuous = false;
  speechRecognition.interimResults = true;
  speechRecognition.maxAlternatives = 1;

  speechRecognition.addEventListener("start", () => {
    voiceRecognitionFailed = false;
    setListening(true);
    selectionCard.hidden = true;
    searchStatus.textContent = "Listening… Speak your search in English.";
    recordClientEvent("voice_start", { language: speechRecognition.lang });
  });

  speechRecognition.addEventListener("result", (event) => {
    let transcript = "";
    for (let index = 0; index < event.results.length; index += 1) {
      transcript += event.results[index][0].transcript;
    }
    input.value = transcript.trim();
    ghostInput.value = "";
    clearButton.hidden = input.value.length === 0;
    searchStatus.textContent = input.value
      ? `Listening… “${input.value}”`
      : "Listening… Speak your search in English.";
    recordClientEvent("voice_result", { transcript: input.value });
  });

  speechRecognition.addEventListener("error", (event) => {
    if (event.error === "aborted") {
      return;
    }
    voiceRecognitionFailed = true;
    searchStatus.textContent = speechErrorMessage(event.error);
    recordClientEvent("voice_error", { error: event.error });
  });

  speechRecognition.addEventListener("end", () => {
    setListening(false);
    recordClientEvent("voice_end", {
      transcript: input.value,
      failed: voiceRecognitionFailed,
    });
    if (input.value.trim().length > 0) {
      clearTimeout(debounceTimer);
      fetchSuggestions(input.value, "voice");
      fetchNextWord(input.value);
    } else if (!voiceRecognitionFailed) {
      searchStatus.textContent = "Voice search ended without a result.";
    }
    input.focus();
  });

  voiceButton.addEventListener("click", () => {
    if (isListening) {
      speechRecognition.stop();
      return;
    }
    if (currentRequest) {
      currentRequest.abort();
    }
    if (nextWordRequest) {
      nextWordRequest.abort();
    }
    ghostInput.value = "";
    clearTimeout(debounceTimer);
    suggestions = [];
    suggestionList.replaceChildren();
    setOpen(false);
    try {
      speechRecognition.start();
    } catch (error) {
      if (error.name !== "InvalidStateError") {
        searchStatus.textContent = "Voice recognition could not start.";
      }
    }
  });
}

async function selectSuggestion(index) {
  const suggestion = suggestions[index];
  if (!suggestion) {
    return;
  }

  try {
    const response = await fetch("/api/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sentence_id: suggestion.sentence_id,
        query: input.value,
        input_method: lastInputMethod,
      }),
    });
    if (!response.ok) {
      throw new Error("Selection request failed");
    }
    const payload = await response.json();
    selectedSentence.textContent = payload.selected.completed_sentence;
    selectedSource.textContent = `${payload.selected.source_text}:${payload.selected.offset} · selected ${payload.selected.usage_count} time${payload.selected.usage_count === 1 ? "" : "s"}`;
    selectionCard.hidden = false;
    input.value = "";
    ghostInput.value = "";
    clearButton.hidden = true;
    suggestions = [];
    suggestionList.replaceChildren();
    searchStatus.textContent = "Selection saved. Ready for a new search.";
    setOpen(false);
    input.focus();
  } catch {
    searchStatus.textContent = "The selection could not be saved";
  }
}

input.addEventListener("input", scheduleSearch);

input.addEventListener("scroll", () => {
  ghostInput.scrollLeft = input.scrollLeft;
});

input.addEventListener("focus", () => {
  if (suggestions.length > 0) {
    setOpen(true);
  }
});

input.addEventListener("keydown", (event) => {
  if (
    event.key === "Tab"
    && ghostInput.value.length > input.value.length
    && ghostInput.value.startsWith(input.value)
  ) {
    event.preventDefault();
    input.value = ghostInput.value;
    ghostInput.value = "";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return;
  }

  if (event.key === "ArrowDown" && suggestions.length > 0) {
    event.preventDefault();
    activeIndex = (activeIndex + 1) % suggestions.length;
    updateActiveSuggestion();
  } else if (event.key === "ArrowUp" && suggestions.length > 0) {
    event.preventDefault();
    activeIndex = activeIndex <= 0 ? suggestions.length - 1 : activeIndex - 1;
    updateActiveSuggestion();
  } else if (event.key === "Enter" && suggestions.length > 0) {
    event.preventDefault();
    selectSuggestion(activeIndex >= 0 ? activeIndex : 0);
  } else if (event.key === "Escape") {
    setOpen(false);
    activeIndex = -1;
  }
});

clearButton.addEventListener("click", () => {
  if (isListening && speechRecognition) {
    speechRecognition.abort();
  }
  input.value = "";
  scheduleSearch();
  input.focus();
});

document.addEventListener("click", (event) => {
  if (!searchShell.contains(event.target)) {
    setOpen(false);
  }
});

configureVoiceSearch();
recordClientEvent("page_view", { page: "search" });
input.focus();
