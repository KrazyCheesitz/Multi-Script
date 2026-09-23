const fs=require('fs'),path=require('path');
const root=path.resolve(__dirname,'..');
const parser=new Function(fs.readFileSync(path.join(root,'extension/core/parser.js'),'utf8')+';return ZSParse;')();
const bridge=fs.readFileSync(path.join(root,'runtime/bridge.py'),'utf8');
const tools=[...bridge.matchAll(/\{["']name["']:\s*["'](ms_[^"']+)["']/g)].map(x=>x[1]);
if(tools.length!==151||new Set(tools).size!==151)throw new Error('expected 151 unique built-in tools, got '+tools.length);
const providers=['arena','chatgpt','deepseek','gemini','glm','kimi','meta','notion','qwen'];
let cases=0;
for(const provider of providers){
 const providerCode=fs.readFileSync(path.join(root,'extension/providers',provider+'.js'),'utf8');
 for(const required of ['typeAndSend','getEditor','conversationKey','installSendHooks'])if(!providerCode.includes(required))throw new Error(provider+' missing '+required);
 for(const tool of tools){
  const args={provider,probe:tool,nested:{items:[1,true,null,'{safe}']}};
  const obj={command:tool,params:args};
  const envelopes=[JSON.stringify(obj),'```json\n'+JSON.stringify(obj,null,2)+'\n```','###MCP_TOOL###\n'+JSON.stringify(obj)+'\n###END_MCP_TOOL###',JSON.stringify({tool,arguments:args})];
  for(const text of envelopes){const out=parser.parseToolCalls(text);if(out.length!==1||out[0].tool!==tool||out[0].arguments.provider!==provider)throw new Error(`${provider}/${tool} failed: ${text.slice(0,30)}`);cases++;}
 }
 for(const name of ['unity::create_asset','godot::create_scene','blender::create_mesh','roblox::execute_luau']){const out=parser.parseToolCalls(JSON.stringify({command:name,params:{provider}}));if(out.length!==1||out[0].tool!==name)throw new Error(provider+' namespaced '+name);cases++;}
}
console.log(`PASS cross-provider tool matrix: ${tools.length} built-ins + 4 namespaced MCP probes across ${providers.length} AI websites (${cases} command envelopes)`);
