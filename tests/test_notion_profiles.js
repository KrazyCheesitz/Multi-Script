const fs = require('fs'), vm = require('vm');
const store = new Map();
const localStorage = { getItem:k=>store.has(k)?store.get(k):null, setItem:(k,v)=>store.set(k,String(v)), removeItem:k=>store.delete(k) };
const context = { console, localStorage, setTimeout, clearTimeout, Event:function(){}, InputEvent:function(){}, KeyboardEvent:function(){}, document:{ querySelectorAll:()=>[], getElementById:()=>null, documentElement:{dataset:{},appendChild(){},classList:{add(){}}}, createElement:()=>({style:{}}) }, window:{ getSelection:()=>({}) }, location:{pathname:'/',search:'',hash:''} };
vm.createContext(context);
const code=fs.readFileSync('extension/providers/notion.js','utf8')+'\n;globalThis.__P=ZSProvider;';
vm.runInContext(code,context);
const P=context.__P;
const profiles=P.autoRoutingProfiles();
if(profiles.map(x=>x.id).join(',')!=='opus55,opus,kimi,gpt') throw new Error('profile list');
for(const id of ['opus55','opus','kimi','gpt']) { P.setAutoRoutingProfile(id); const prompt=P.getStartupProfilePrompt(); if(!prompt || !prompt.toLowerCase().includes(id==='gpt'?'gpt-5.6 sol':id==='kimi'?'kimi k3':id==='opus55'?'claude opus 5.5':'claude opus 5')) throw new Error(id); }
localStorage.setItem('zs.notion.modelPreference','Claude Opus 5'); P.setAutoRoutingProfile('kimi');
if(localStorage.getItem('zs.notion.modelPreference')!==null) throw new Error('stale direct preference');
P.setAutoRoutingProfile(''); if(P.getStartupProfilePrompt()!=='') throw new Error('disable');
console.log('PASS Notion Auto starter profiles');
