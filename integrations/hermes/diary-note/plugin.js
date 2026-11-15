import { jsx, jsxs } from 'react/jsx-runtime';

const LABELS = {
  public: '公账已记',
  private: '私账已记',
};

const TIME_RE = /^\d{1,2}:\d{2}$/;
const MAX_TEXT_LEN = 120;

function normalizeLedger(raw) {
  if (typeof raw !== 'string') return '';
  const key = raw.trim().toLowerCase();
  return key === 'public' || key === 'private' ? key : '';
}

function normalizeTime(raw) {
  if (typeof raw !== 'string') return '';
  const val = raw.trim();
  return TIME_RE.test(val) ? val : '';
}

function normalizeText(raw) {
  if (typeof raw !== 'string') return '';
  const cleaned = raw.replace(/[\r\n]+/g, ' ').replace(/\s+/g, ' ').trim();
  if (!cleaned) return '';
  return cleaned.length > MAX_TEXT_LEN
    ? `${cleaned.slice(0, MAX_TEXT_LEN)}…`
    : cleaned;
}

function renderDirective(props) {
  try {
    const attrs = props && typeof props.attrs === 'object' && props.attrs !== null
      ? props.attrs
      : {};
    const ledger = normalizeLedger(attrs.ledger);
    const label = LABELS[ledger] || '日记已记';
    const time = normalizeTime(attrs.time);
    const text = normalizeText(attrs.text);
    const meta = [time, text].filter(Boolean).join(' · ');

    const children = [
      jsx('span', {
        style: { opacity: 0.9, flexShrink: 0 },
        children: '📔',
      }),
      jsx('span', {
        style: {
          color: 'var(--ui-color-accent, #b58a2b)',
          fontWeight: 500,
          flexShrink: 0,
        },
        children: label,
      }),
    ];

    if (meta) {
      children.push(
        jsx('span', {
          style: {
            color: 'var(--tool-memory-legendary-meta, var(--ui-text-secondary, #8a8a8a))',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          },
          children: meta,
        }),
      );
    }

    return jsxs('div', {
      style: {
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
        fontSize: '12px',
        padding: '2px 0',
        textAlign: 'left',
        whiteSpace: 'nowrap',
      },
      children,
    });
  } catch {
    return '日记已记';
  }
}

export default {
  id: 'diary-note',
  name: 'Diary Note',
  description: 'Render diary-note directives in conversation transcripts.',
  register(ctx) {
    ctx.register({
      id: 'diary-note-directive',
      area: 'transcript.directives',
      data: {
        name: 'diary-note',
        render: renderDirective,
      },
    });
  },
};
