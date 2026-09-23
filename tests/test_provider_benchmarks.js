const fs=require('fs'),vm=require('vm');const c={console};vm.createContext(c);
vm.runInContext(fs.readFileSync('extension/core/config.js','utf8')+'\n;globalThis.__MS=ZS;',c);
const rows=c.__MS.PROVIDER_BENCHMARKS;if(!Array.isArray(rows)||rows.length!==7)throw new Error('expected seven benchmarked providers');
const ids=rows.map(x=>x.id);for(const id of ['deepseek','chatgpt','gemini','kimi','glm','qwen','meta'])if(!ids.includes(id))throw new Error('missing '+id);
for(const id of ['arena','notion'])if(ids.includes(id))throw new Error('must exclude '+id);
for(const x of rows){for(const k of ['speed','smart','stability'])if(!Number.isInteger(x[k])||x[k]<1||x[k]>10)throw new Error(`${x.id}.${k}`);if(!x.note)throw new Error('missing note '+x.id)}
const main=fs.readFileSync('extension/core/main.js','utf8');for(const x of ['Provider benchmarks','Speed','Smart','Stability','Arena and Notion AI are excluded','ms-bench-card'])if(!main.includes(x))throw new Error('UI missing '+x);
console.log('PASS seven-provider 1–10 benchmark dashboard excluding Arena and Notion');
