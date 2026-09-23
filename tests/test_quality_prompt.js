const fs=require('fs'),vm=require('vm'); const c={console}; vm.createContext(c);
vm.runInContext(fs.readFileSync('extension/core/config.js','utf8')+'\n;globalThis.__ZS=ZS;',c);
for(const level of ['polished','ambitious']) { const p=c.__ZS.qualityAmplifierPrompt(level); if(!p.includes('QUALITY AMPLIFIER')||!p.includes('ms_enhance_brief')||p.length<450) throw new Error(level); }
if(c.__ZS.qualityAmplifierPrompt('off')!=='') throw new Error('off');
console.log('PASS quality amplifier prompt levels');
