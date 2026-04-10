import re


def _split_sentences(text):
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if sentence.strip()
    ]


def extract_speaker_metadata(text):
    speaker_stats = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        match = re.match(r"^([A-Z][a-zA-Z]+):\s*(.+)$", line)
        if not match:
            continue

        speaker = match.group(1)
        utterance = match.group(2).strip()
        words = [w for w in re.findall(r"\w+", utterance) if w]

        stats = speaker_stats.setdefault(
            speaker,
            {
                "speaker": speaker,
                "utterances": 0,
                "words": 0,
            },
        )
        stats["utterances"] += 1
        stats["words"] += len(words)

    speakers = sorted(speaker_stats.values(), key=lambda item: item["speaker"])
    return {
        "speaker_count": len(speakers),
        "speakers": speakers,
    }


def _classify_sentiment(sentence):
    text = sentence.lower()

    frustration_markers = [
        "frustrated",
        "frustration",
        "stuck",
        "annoyed",
        "blocked",
    ]
    conflict_markers = [
        "concern",
        "concerned",
        "issue",
        "risk",
        "problem",
        "delay",
        "disagree",
        "conflict",
    ]
    agreement_markers = [
        "agreed",
        "agree",
        "decided",
        "approved",
        "confirmed",
        "aligned",
    ]

    if any(marker in text for marker in frustration_markers):
        return "frustration"
    if any(marker in text for marker in conflict_markers):
        return "conflict"
    if any(marker in text for marker in agreement_markers):
        return "agreement"
    return "neutral"


def analyze_sentiment(text):
    timeline = []
    speaker_summary = {}
    current_speaker = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = re.match(r"^([A-Z][a-zA-Z]+):\s*(.+)$", line)
        if match:
            current_speaker = match.group(1)
            content = match.group(2).strip()
        else:
            # Skip title/metadata lines before the first speaker turn.
            if current_speaker is None:
                continue
            content = line

        for sentence in _split_sentences(content):
            speaker = current_speaker or "Unknown"
            label = _classify_sentiment(sentence)

            timeline.append(
                {
                    "index": len(timeline) + 1,
                    "speaker": speaker,
                    "sentiment": label,
                    "text": sentence,
                }
            )

            summary = speaker_summary.setdefault(
                speaker,
                {
                    "speaker": speaker,
                    "agreement": 0,
                    "conflict": 0,
                    "frustration": 0,
                    "neutral": 0,
                },
            )
            summary[label] += 1

    summary_list = sorted(speaker_summary.values(), key=lambda item: item["speaker"])

    totals = {
        "agreement": sum(item["agreement"] for item in summary_list),
        "conflict": sum(item["conflict"] for item in summary_list),
        "frustration": sum(item["frustration"] for item in summary_list),
        "neutral": sum(item["neutral"] for item in summary_list),
    }

    return {
        "timeline": timeline,
        "speaker_summary": summary_list,
        "totals": totals,
    }
