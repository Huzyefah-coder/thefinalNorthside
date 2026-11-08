// Voice-only normalization for the existing human handoff conversation.
// Do not change backend input rules or the normal typed chat.
const digits = {zero:'0', oh:'0', o:'0', one:'1', two:'2', three:'3', four:'4', five:'5', six:'6', seven:'7', eight:'8', nine:'9'};
const small = {ten:'10', eleven:'11', twelve:'12', thirteen:'13', fourteen:'14', fifteen:'15', sixteen:'16', seventeen:'17', eighteen:'18', nineteen:'19'};

function spokenPhone(text) {
  let cleaned = text.toLowerCase().trim()
    .replace(/^(?:(?:my|the)\s+(?:contact\s+)?(?:phone\s+)?number\s+(?:is|it's)\s+|(?:it is|it's)\s+)/, '')
    .replace(/[–—]/g, '-');
  if (/^\+?[\d\s().-]+$/.test(cleaned)) return cleaned.replace(/[\s().-]/g, '');
  const tokens = cleaned.replace(/[,.;:!?()\-]/g, ' ').split(/\s+/).filter(Boolean);
  let result = '';
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (token === 'plus' && i === 0) { result += '+'; continue; }
    if (token === 'double' || token === 'triple') {
      const d = digits[tokens[++i]];
      if (d === undefined) return text;
      result += d.repeat(token === 'double' ? 2 : 3);
    } else if (digits[token] !== undefined) result += digits[token];
    else if (/^\d+$/.test(token)) result += token;
    else if (small[token] !== undefined) result += small[token];
    else return text;
  }
  // Only send valid-looking numeric strings. The server retains final validation.
  return /^\+?\d{10,15}$/.test(result) ? result : text;
}

function answerForHandoff(text) {
  const answer = text.toLowerCase().trim().replace(/[.,!?]/g, ' ').replace(/\s+/g, ' ').trim();
  if (/^(?:yes|yeah|yep|yup|yes please|yes confirm|yes i confirm|yes i accept|yes i do|i do|sure|please|go ahead|submit|submit it|please submit|okay|ok|confirm|confirm it|that's correct|that is correct|correct|please do|you can submit(?: it)?|yes go ahead)$/.test(answer)) return 'yes';
  if (/^(?:no|nope|no thanks|no thank you|not now|never mind|nevermind|do not submit|don't submit|cancel|cancel it|stop)$/.test(answer)) return 'no';
  return text;
}

export function normalizeLiveVoiceInput(transcript, lastReply) {
  const raw = transcript.trim();
  const prompt = (lastReply || '').toLowerCase();
  // Include the *retry* prompts, not just the first prompt. The previous version
  // normalized the first response and then stopped on subsequent invalid replies.
  const askingPhone = /what phone number should clinic staff use to contact you|please provide a valid contact number|contact number, for example/.test(prompt);
  const askingConfirmation = /shall i submit this request|please say yes to submit|please say yes or no|would you like me to record a staff follow-up request/.test(prompt);
  if (askingPhone) return spokenPhone(raw);
  if (askingConfirmation) return answerForHandoff(raw);
  return raw;
}
