const fs=require('fs');
const main=fs.readFileSync('extension/core/main.js','utf8');
const notion=fs.readFileSync('extension/providers/notion.js','utf8');
for(const x of ['const bootstrapClaims = new Set()','bootstrapClaims.has(claimKey)','bootstrapClaims.add(claimKey)','if (!bootstrapAccepted) bootstrapClaims.delete(claimKey)','once per new chat','Saved for the next new chat']) if(!main.includes(x)) throw new Error('missing '+x);
for(const x of ['notionPageToken','notionChatEpoch','notionHadTurns','notionChatEpoch += 1']) if(!notion.includes(x)) throw new Error('missing '+x);
if(/resendSystemEvery\s*:/.test(notion)) throw new Error('Notion must not periodically resend full system prompt');
console.log('PASS one Notion bootstrap per chat and next-chat profile lifecycle');
