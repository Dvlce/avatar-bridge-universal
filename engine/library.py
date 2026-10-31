"""Pack local, compiled bodies into one installable player model library."""
from pathlib import Path
import copy
import hashlib
import json
import re
import shutil
import uuid
import bridge as b
import cp77 as c
import yaml


SCRIPT = r'''
local catalog = json.decode(CATALOG_JSON)
local selected = json.decode(DEFAULT_JSON)
local visible = false
local elapsed = 0
local previews = {}
local errorText = ""
local originals = {}
local resources = {}
local refitCount = 0
local errors = {}
local expression = "neutral"
local faceClock = 0
local faceElapsed = 0
local faceResources = {}
local function hideMesh(comp)
  if comp.chunkMask~=0 then comp.chunkMask=0;comp:RefreshAppearance() end
  comp:Toggle(false)
end
local function warn(message)
  message=tostring(message)
  if errors[message] then return end
  errors[message]=true
  local f=io.open("runtime-errors.log","a")
  if f then f:write(message .. "\n");f:close() end
end
local function hash(reference)
  return tostring(ResRef.GetHash(reference)):gsub("[uUlL]+$","")
end
local chunkChecks={}
local function chunksActive(mask,count)
  if not count or count<1 or count>64 then return false end
  local key=tostring(mask) .. ":" .. count
  if chunkChecks[key]~=nil then return chunkChecks[key] end
  -- Keep native uint64 exact: converting an all-bits mask to a Lua number
  -- loses low bits. Decimal long division only operates on single digits.
  local digits=tostring(mask):match("^(%d+)")
  if not digits then return false end
  for _=1,count do
    local quotient="";local remainder=0
    for i=1,#digits do
      local value=remainder*10+tonumber(digits:sub(i,i))
      local digit=math.floor(value/2);remainder=value%2
      if digit~=0 or #quotient>0 then quotient=quotient .. digit end
    end
    if remainder==0 then chunkChecks[key]=false;return false end
    digits=#quotient>0 and quotient or "0"
  end
  chunkChecks[key]=true;return true
end
local function componentEnabled(component)
  local ok,enabled=pcall(function() return component.isEnabled end)
  if not ok or type(enabled)~="boolean" then
    warn("Stato componente non disponibile: copertura ignorata")
    return false
  end
  return enabled
end
local function fitComponents(entity,item)
  if not entity or not item.garments then return end
  local identity=tostring(entity:GetEntityID().hash)
  for _,comp in ipairs(entity:GetComponents()) do
    local hasMesh,mesh=pcall(function() return comp.meshResource end)
    if hasMesh and mesh then
    local success,message=pcall(function()
      local key=identity .. ":" .. NameToString(comp.name) .. ":" .. tostring(comp.id)
      local current=hash(mesh.path)
      local original=originals[key]
      if original and current~=original.hash and item.garments[current] then
        original=nil;originals[key]=nil
      end
      local source=original and original.hash or current
      local fit=item.garments[source]
      local componentName=NameToString(comp.name)
      if (componentName:match("^[tls]%d_") and componentName:find("_shadow",1,true)) or componentName:match("^s[1-9]_%d") or
         (fit and fit.source:find("\\feet\\",1,true)) or
         (item.hiddenFootwear and item.hiddenFootwear[current]) then
        hideMesh(comp);return
      end
      if not fit then return end
      if not componentEnabled(comp) then return end
      local desired=ResRef.FromString(fit.mesh)
      local desiredHash=hash(desired)
      if current==desiredHash then return end
      if not original then
        original={hash=current};originals[key]=original
      end
      local token=resources[fit.mesh]
      if not token then token=Game.GetResourceDepot():LoadResource(desired);resources[fit.mesh]=token end
      if not token or not token:IsFinished() then return end
      if not token:IsLoaded() then warn("Refit non caricato: " .. fit.mesh);return end
      local ok,err=pcall(function()
        if not comp:ChangeResource(desired,true) then error("Refit non applicato: " .. fit.mesh) end
      end)
      if not ok then
        warn(err)
      else
        resources[fit.mesh]=nil
        refitCount=refitCount+1
        local f=io.open("refit-state.json","w")
        if f then f:write(json.encode({model=item.id,replacements=refitCount,last_source=fit.source,last_target=fit.mesh}));f:close() end
      end
    end)
    if not success then warn(message) end
    end
  end
end
local function fitClothes(entity,item)
  if not entity then return end
  fitComponents(entity,item)
  for _,slot in ipairs({"Head","Face","Torso","Chest","InnerChest","OuterChest","Legs","Feet","Outfit","UnderwearTop","UnderwearBottom"}) do
    pcall(function()
      local attachment=Game.GetTransactionSystem():GetItemInSlot(entity,TweakDBID.new("AttachmentSlots." .. slot))
      if attachment then
        if slot=="Feet" then
          for _,comp in ipairs(attachment:GetComponents()) do
            pcall(function() if comp.meshResource then hideMesh(comp) end end)
          end
        else fitComponents(attachment,item) end
      end
    end)
  end
  if item.coverage then
    local covered=false
    local hidden={}
    local rows={}
    local seen={}
    local function collect(owner)
      if not owner then return end
      for _,component in ipairs(owner:GetComponents()) do
        pcall(function()
          if component.meshResource then
            local key=hash(component.meshResource.path)
            local regions=item.coverageGarments[key]
            if regions then
              local enabled=componentEnabled(component)
              local whole=chunksActive(component.chunkMask,item.coverageChunkCounts[key])
              local identity=tostring(owner:GetEntityID().hash) .. ":" .. tostring(component.id)
              if not seen[identity] then
                seen[identity]=true
                table.insert(rows,{name=NameToString(component.name),hash=key,
                  mask=tostring(component.chunkMask),enabled=enabled,whole=whole,regions=regions})
              end
              if enabled and whole then
                for _,region in ipairs(regions) do hidden[region]=true end
                if item.lowerGarments[key] then covered=true end
              end
            end
          end
        end)
      end
    end
    collect(entity)
    pcall(function()
      local function attachments(owner)
        local found=false
        for _,slot in ipairs({"Torso","Chest","InnerChest","OuterChest","Legs","UnderwearTop","UnderwearBottom","Outfit"}) do
          local attachment=Game.GetTransactionSystem():GetItemInSlot(owner,TweakDBID.new("AttachmentSlots." .. slot))
          if attachment then
            found=true
            collect(attachment)
            if slot=="Legs" or slot=="UnderwearBottom" then covered=true end
          end
        end
        return found
      end
      -- A preview can own its own equipment. Use player equipment only when
      -- the preview has no attachments, never merge two different outfits.
      local found=attachments(entity)
      local player=Game.GetPlayer()
      if not found and player and player~=entity then attachments(player) end
    end)
    local signature=json.encode(rows)
    local label=entity:FindComponentByName(CName.new(item.head)) and "paperdoll" or "player"
    item.coverageAudit=item.coverageAudit or {}
    if item.coverageAudit[label]~=signature then
      item.coverageAudit[label]=signature
      local f=io.open("coverage-components-" .. label .. ".json","w")
      if f then f:write(signature);f:close() end
    end
    local part=entity:FindComponentByName(CName.new(item.head)) and "body" or "body_fpp"
    if covered and not item.compactPelvis then hidden.pelvis=true end
    if item.intactSkin then
      for region in pairs(hidden) do if not region:match("^[lr]leg_%d$") then hidden[region]=nil end end
    end
    if covered then hidden.pelvis=true end
    for _,target in ipairs({{part=part,name=item.body},{part="arms_fpp",name=item.arms}}) do
      local masks=item.coverage[target.part]
      local comp=target.name and entity:FindComponentByName(CName.new(target.name))
      if masks and comp then
        local desired=masks.full
        for _,zone in ipairs(masks.zones or {}) do
          if hidden[zone.region] then desired=desired-zone.bit end
        end
        if comp.chunkMask~=desired then
          comp.chunkMask=desired;comp:RefreshAppearance()
          local f=io.open("coverage-state.json","w")
          if f then f:write(json.encode({model=item.id,part=target.part,lower_clothing=covered,mask=desired,hidden_regions=hidden}));f:close() end
        end
      end
    end
  end
end
local audited = {}
local function audit(entity,label)
  if not entity or audited[label] then return end
  local result={}
  for _,comp in ipairs(entity:GetComponents()) do
    pcall(function()
      local mesh=comp.meshResource
      if not mesh then return end
      local row={name=NameToString(comp.name),hash=hash(mesh.path),mask=tostring(comp.chunkMask),appearances={}}
      for _,app in ipairs(mesh.appearances) do
        local mats={};for _,mat in ipairs(app.chunkMaterials) do table.insert(mats,NameToString(mat)) end
        table.insert(row.appearances,{name=NameToString(app.name),materials=mats})
      end
      table.insert(result,row)
    end)
  end
  if #result>0 then
    local f=io.open("components-" .. label .. ".json","w")
    if f then f:write(json.encode(result));f:close();audited[label]=true end
  end
end
local function save()
  local f = io.open("selection.json", "w")
  if f then f:write(json.encode({id=selected})); f:close() end
end
local function updateFace(entity,item)
  if not entity or not item or not item.face then return end
  local comp=entity:FindComponentByName(CName.new(item.head))
  if not comp then return end
  local choice=item.face.expressions[1]
  for _,candidate in ipairs(item.face.expressions) do
    if candidate.id==expression then choice=candidate end
  end
  if not choice then return end
  local state=1
  if item.face.blink and #choice.states==3 then
    local phase=faceClock%4.3
    if phase<.06 or (phase>=.14 and phase<.20) then state=2
    elseif phase<.14 then state=3 end
  end
  local path=choice.states[state]
  if not path then return end
  local desired=ResRef.FromString(path)
  if hash(comp.meshResource.path)==hash(desired) then return end
  local token=faceResources[path]
  if not token then token=Game.GetResourceDepot():LoadResource(desired);faceResources[path]=token end
  if not token or not token:IsFinished() then return end
  if not token:IsLoaded() then warn("Espressione non caricata: " .. path);return end
  if not comp:ChangeResource(desired,true) then warn("Espressione non applicata: " .. path);return end
  local f=io.open("face-state.json","w")
  if f then f:write(json.encode({model=item.id,expression=choice.id,blink_state=state,mesh=path}));f:close() end
end
local function available(entity, item)
  return entity and entity:FindComponentByName(CName.new(item.body)) ~= nil
end
local function apply(entity)
  if not entity then return false end
  local active = nil
  for _, item in ipairs(catalog) do
    if available(entity,item) and (not active or item.id == selected) then active = item end
  end
  if not active then return false end
  audit(entity,entity:FindComponentByName(CName.new(active.head)) and "paperdoll" or "player")
  for _,comp in ipairs(entity:GetComponents()) do
    local n=NameToString(comp.name)
    if n=="torso" or n=="legs" or n=="shoes" or n=="body" or n=="head_shadowmesh" or
       n=="left_arm" or n=="right_arm" or (n:match("^a0_%d") and not n:find("cuff",1,true)) then
      pcall(function()
        if comp.meshResource then
          if comp.chunkMask~=0 then comp.chunkMask=0;comp:RefreshAppearance() end
          comp:Toggle(false)
        end
      end)
    end
  end
  for _, item in ipairs(catalog) do
    for _, part in ipairs({item.body,item.head,item.arms or item.body,item.tail,item.tailRig}) do
      local comp = entity:FindComponentByName(CName.new(part))
      if comp then comp:Toggle(item.id == active.id) end
    end
  end
  if not entity:FindComponentByName(CName.new(active.head)) then
    pcall(function()
      local body=entity:FindComponentByName(CName.new(active.arms or active.body))
      if NameToString(body.renderingPlaneAnimationParam)~="renderPlane" then
        body.renderingPlaneAnimationParam=CName.new("renderPlane");body:RefreshAppearance()
      end
    end)
  end
  return active
end
local function refresh()
  local player=Game.GetPlayer()
  local active=apply(player)
  if active then fitClothes(player,active) end
  for _, p in pairs(previews) do pcall(function() local item=apply(p);if item then fitClothes(p,item) end end) end
end
registerForEvent("onInit", function()
  local f = io.open("selection.json","r")
  if f then
    local ok,value = pcall(json.decode,f:read("*a")); f:close()
    if ok and type(value)=="table" then
      for _,item in ipairs(catalog) do if item.id==value.id then selected=value.id end end
    end
  end
  ObserveAfter("gameuiInGameMenuGameController","OnPuppetReady",function(_,scene,puppet)
    if puppet then previews[tostring(scene)] = puppet; apply(puppet) end
  end)
  print("Avatar Bridge: body library loaded (" .. #catalog .. ")")
end)
registerForEvent("onOverlayOpen", function() visible=true end)
registerForEvent("onOverlayClose", function() visible=false end)
registerForEvent("onUpdate", function(delta)
  faceClock=faceClock+delta;faceElapsed=faceElapsed+delta
  if faceElapsed>=.03 then
    faceElapsed=0
    local item=nil
    for _,candidate in ipairs(catalog) do if candidate.id==selected then item=candidate end end
    if item then
      pcall(function() updateFace(Game.GetPlayer(),item) end)
      for _,puppet in pairs(previews) do
        local ok,message=pcall(function() updateFace(puppet,item) end)
        if not ok then warn(message) end
      end
    end
  end
  elapsed=elapsed+delta
  if elapsed < .5 then return end
  elapsed=0
  refresh()
end)
registerForEvent("onDraw", function()
  if not visible then return end
  ImGui.SetNextWindowSize(340,360,ImGuiCond.FirstUseEver)
  if ImGui.Begin("Avatar") then
    local groups = {}
    for _,item in ipairs(catalog) do
      if not groups[item.category] then groups[item.category]={} end
      table.insert(groups[item.category],item)
    end
    local names={};for category,_ in pairs(groups) do table.insert(names,category) end;table.sort(names)
    for _,category in ipairs(names) do
      if ImGui.CollapsingHeader(category,ImGuiTreeNodeFlags.DefaultOpen) then
        for _,item in ipairs(groups[category]) do
          if ImGui.Selectable(item.name .. "##" .. item.id,selected==item.id) then
            if available(Game.GetPlayer(),item) then
              selected=item.id;errorText="";save();refresh()
            elseif not Game.GetPlayer() then errorText="Carica un salvataggio."
            else errorText="Richiede V " .. (item.gender=="male" and "maschile" or "femminile") end
          end
        end
      end
    end
    for _,item in ipairs(catalog) do
      if item.id==selected and item.face then
        ImGui.Separator()
        for _,choice in ipairs(item.face.expressions) do
          if ImGui.RadioButton(choice.name .. "##face",expression==choice.id) then expression=choice.id end
        end
      end
    end
    if errorText~="" then ImGui.TextWrapped(errorText) end
  end
  ImGui.End()
end)
return {
  SelectAvatar=function(id)
    for _,item in ipairs(catalog) do
      if item.id==id and available(Game.GetPlayer(),item) then
        selected=id;save();refresh();return true
      end
    end
    return false
  end
}
'''


def lua_literal(value):
    encoded=json.dumps(value,ensure_ascii=True)
    separator="="
    while "]"+separator+"]" in encoded: separator+="="
    return "["+separator+"["+encoded+"]"+separator+"]"


def catalog_entry(job, report):
    key=job.name.removeprefix("avatar_")
    label=str(report.get("name",Path(report["selected_base"]).stem))[:80]
    gender=report["gender"]
    return {"id":job.name,"name":label+" · "+("V M" if gender=="male" else "V F"),
            "category":str(report.get("category","Avatar"))[:60],"gender":gender,
            "body":"avatarbridge_"+key+"_body","head":"avatarbridge_"+key+"_head"}


def footwear_hashes(garments):
    from garments import fnv_path
    hidden={}
    for original,fit in garments.items():
        if '/feet/' in fit['source'].replace('\\','/').lower():
            hidden[str(original)]=True;hidden[fnv_path(fit['mesh'])]=True
    return hidden


def lower_garment_hashes(garments):
    from garments import fnv_path
    hidden={}
    for original,fit in garments.items():
        source=fit['source'].replace('\\','/').lower()
        if '/legs/' in source and 'shadow' not in source:
            hidden[str(original)]=True;hidden[fnv_path(fit['mesh'])]=True
    return hidden


def clothing_coverage_hashes(garments, compact_pelvis=False, intact_skin=False):
    from garments import fnv_path
    def regions(fit):
        if intact_skin:return [region for region in fit.get('hide_regions',[]) if re.fullmatch(r'[lr]leg_\d',region)]
        import garments as g
        return g.safe_hidden_regions(fit.get('hide_regions',[]),fit['source'],compact_pelvis)
    return {key:regions(fit) for original,fit in garments.items()
            for key in [str(original),fnv_path(fit['mesh'])]}


def clothing_chunk_counts(garments):
    from garments import fnv_path
    return {key:int(fit.get('coverage_chunk_count',0)) for original,fit in garments.items()
            for key in [str(original),fnv_path(fit['mesh'])]}


def body_component(item, part, prefix, enabled, mesh_part=None):
    comp=c.component(part,1 if part=="body" else 2)
    comp["name"]=c.name(item[part])
    comp["id"]=str(1800000000000000000+int(hashlib.sha256((item['id']+part).encode()).hexdigest()[:14],16))
    comp["mesh"]=c.ref(prefix+"\\"+(mesh_part or part)+".mesh")
    comp["isEnabled"]=int(enabled)
    return comp


def build(selected_job, game):
    selected_job=Path(selected_job).resolve();game=Path(game)
    entries=[];seen=set()
    candidates=sorted((b.DATA/"jobs").glob("*/build.json"))
    for p in candidates:
        info=json.loads(p.read_text(encoding="utf-8"))
        if info.get("builder_version",0)<21:continue
        job=p.parent
        report=json.loads((job/"report.json").read_text(encoding="utf-8"))
        identity=(report["sha256"],report["gender"])
        if identity in seen and job!=selected_job:continue
        seen.add(identity)
        package=Path(info["package"]).resolve()
        if not package.is_relative_to(job.resolve()):raise ValueError("Pacchetto fuori dalla libreria")
        if hashlib.sha256((package/(c.MOD+".archive")).read_bytes()).hexdigest()!=info["sha256"]:
            raise ValueError("Una base compilata è stata modificata: "+job.name)
        item=catalog_entry(job,report)
        item["garments"]=info.get("garments",{})
        item['hiddenFootwear']=footwear_hashes(item['garments'])
        if info.get('face'):item['face']=info['face']
        item['coverage']=info.get('coverage',{})
        item['compactPelvis']=info.get('pelvis_profile_version',0)>=1
        item['intactSkin']=info.get('intact_skin_version',0)>=1
        item['coverageGarments']=clothing_coverage_hashes(item['garments'],item['compactPelvis'],item['intactSkin'])
        item['coverageChunkCounts']=clothing_chunk_counts(item['garments'])
        item['lowerGarments']=lower_garment_hashes(item['garments'])
        if info.get('first_person'):item['arms']=item['body']+'_arms_fpp'
        if info.get("tail"):
            item["tail"]="avatarbridge_"+job.name.removeprefix("avatar_")+"_tail"
            item["tailRig"]=item["tail"]+"_rig"
        entries.append((job,report,info,item))
    if not any(job==selected_job for job,_,_,_ in entries):
        raise ValueError("Ricompila la base con il builder aggiornato")
    working=selected_job/("library_"+uuid.uuid4().hex[:8]);archive=working/"archive";package=working/"package"
    archive.mkdir(parents=True);package.mkdir()
    patches={};catalog=[]
    for job,report,info,item in entries:
        prefix=b.clean_entry(info["resource_prefix"])
        if prefix != "avatarbridge/"+job.name:raise ValueError("Namespace del corpo non valido")
        # Read cooked resources back from the verified archive, rather than
        # trusting loose build intermediates that may have changed afterward.
        c.command("unbundle",Path(info["package"])/(c.MOD+".archive"),"-o",archive,"-r",r"\.(mesh|xbm|app|rig|animgraph)$")
        dest=archive/Path(prefix)
        if not (dest/"body.mesh").is_file() or not (dest/"head.mesh").is_file():
            raise ValueError("Mesh del corpo compilato mancanti")
        own=yaml.safe_load((Path(info["package"])/(c.MOD+".archive.xl")).read_text())
        patches.update({key:value for key,value in own["resource"]["patch"].items() if key.endswith(".app")})
        catalog.append(item)
    destination=archive/"avatarbridge/library";destination.mkdir(parents=True)
    for gender in sorted({item["gender"] for item in catalog}):
        same=[e for e in entries if e[3]["gender"]==gender]
        short="ma" if gender=="male" else "wa"
        template=json.loads((b.DATA/"build-reference"/gender/f"player_{short}_fpp.ent.json").read_text(encoding="utf-8-sig"))
        default=next((e[3]["id"] for e in same if e[0]==selected_job),same[0][3]["id"])
        for perspective in ["fpp","tpp"]:
            components=[]
            for _,_,info,item in same:
                for part in (["body"] if perspective=="fpp" else ["body","head"]):
                    mesh_part='body_fpp' if perspective=='fpp' and info.get('first_person') else part
                    comp=body_component(item,part,info["resource_prefix"],item["id"]==default,mesh_part)
                    components.append(comp)
                if perspective=='fpp' and info.get('first_person'):
                    arms=body_component(item,'arms',info['resource_prefix'],item['id']==default,'arms_fpp')
                    arms['renderingPlaneAnimationParam']=c.name('renderPlane');components.append(arms)
                if info.get("tail"):
                    import physics
                    tail=body_component(item,"tail",info["resource_prefix"],item["id"]==default)
                    tail["skinning"]["Data"]["bindName"]=c.name(item["tailRig"])
                    component_id=1800000000000000000+int(hashlib.sha256((item['id']+'rig').encode()).hexdigest()[:14],16)
                    components.extend([physics.animated_component(item["tailRig"],info["resource_prefix"],component_id,item["id"]==default),tail])
            filename=f"player_{short}_{perspective}.ent"
            c.patch_ent(template,components,filename,destination)
            reference=next(e[2] for e in same)
            config=yaml.safe_load((Path(reference["package"])/(c.MOD+".archive.xl")).read_text())
            targets=next(value for key,value in config["resource"]["patch"].items() if key.endswith(f"player_{perspective}.ent"))
            patches["avatarbridge\\library\\"+filename]=targets
    for p in destination.glob("*.json"):p.unlink()
    c.command("pack",archive,"-o",package)
    output=package/(c.MOD+".archive")
    (package/"archive.archive").replace(output)
    (package/(c.MOD+".archive.xl")).write_text(yaml.safe_dump({"resource":{"patch":patches}},sort_keys=False))
    script=SCRIPT.replace("CATALOG_JSON",lua_literal(catalog)).replace("DEFAULT_JSON",lua_literal(selected_job.name))
    (package/"init.lua").write_text(script,encoding="utf-8")
    result={"package":str(package),"sha256":hashlib.sha256(output.read_bytes()).hexdigest(),"catalog":catalog}
    (working/"library.json").write_text(json.dumps(result,indent=2))
    return result
