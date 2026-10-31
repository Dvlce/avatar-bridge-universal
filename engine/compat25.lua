local item = json.decode(CATALOG_JSON)
local elapsed = 0
local previews = {}
local visible = false
local expression = "neutral"
local faceClock, faceElapsed = 0, 0
local reported = {}
local function persist(name, value)
  local f = io.open(name, "w")
  if f then f:write(json.encode(value)); f:close() end
end
local function warn(message)
  message = tostring(message)
  if reported[message] then return end
  reported[message] = true
  local f = io.open("runtime-errors.log", "a")
  if f then f:write(message .. "\n"); f:close() end
end
local function hash(value)
  return tostring(value):match("^(%d+)") or "0"
end
local function setMask(component,value)
  if hash(component:ABGetMask()) ~= tostring(value) then component:ABSetMask(value) end
end
local function whole(mask, count)
  if not count or count < 1 or count > 64 then return false end
  local digits = hash(mask)
  for _ = 1, count do
    local quotient, remainder = "", 0
    for i = 1, #digits do
      local value = remainder * 10 + tonumber(digits:sub(i,i))
      local digit = math.floor(value/2); remainder = value%2
      if digit ~= 0 or #quotient > 0 then quotient = quotient .. digit end
    end
    if remainder == 0 then return false end
    digits = #quotient > 0 and quotient or "0"
  end
  return true
end
local function components(entity)
  return entity and entity:ABGetComponents() or {}
end
local function human(name)
  if name:find("avatarbridge_",1,true) then return false end
  return name == "body" or name == "torso" or name == "legs" or name == "shoes"
    or name == "left_arm" or name == "right_arm" or name == "head_shadowmesh"
    or name:find("MorphTargetSkinnedMesh",1,true)
    or name:match("^t0_000_p[wm]a_") or name:match("^a0_%d")
    or name:match("^h[0ehbtx]_0") or name:match("^heb_") or name:match("^hh_")
    or name:match("^ha_") or name:match("^hb_")
end
local function apply(entity)
  if not entity then return end
  local own = components(entity)
  local body, arms, head
  for _, comp in ipairs(own) do
    local name = NameToString(comp:ABGetName())
    if name == item.body then body = comp end
    if name == item.arms then arms = comp end
    if name == item.head then head = comp end
  end
  if not body then return end
  for _, comp in ipairs(own) do
    if human(NameToString(comp:ABGetName())) then setMask(comp,0) end
  end
  local hidden, rows, seen = {}, {}, {}
  local lower = false
  local function collect(owner, footwear)
    for _, comp in ipairs(components(owner)) do
      local mesh = hash(comp:ABGetMeshHash())
      local name = NameToString(comp:ABGetName())
      local shadow = name:match("^[tls]%d_") and name:find("_shadow",1,true)
      if shadow or footwear or item.hiddenFootwear[mesh] or name:match("^s[1-9]_%d") then
        setMask(comp,0)
      else
        local regions = item.coverageGarments[mesh]
        if regions then
          local enabled = comp:ABIsEnabled()
          local mask = comp:ABGetMask()
          local complete = whole(mask, item.coverageChunkCounts[mesh])
          local key = tostring(owner:GetEntityID().hash) .. ":" .. name
          if not seen[key] then
            seen[key] = true
            rows[#rows+1] = {name=name, hash=mesh, enabled=enabled, mask=tostring(mask), whole=complete}
          end
          if enabled and complete then
            for _, region in ipairs(regions) do hidden[region] = true end
            if item.lowerGarments[mesh] then lower = true end
          end
        end
      end
    end
  end
  collect(entity, false)
  local function attachments(owner)
    local found = false
    for _, slot in ipairs({"Torso","Chest","InnerChest","OuterChest","Legs","Feet","UnderwearTop","UnderwearBottom","Outfit"}) do
      local attachment = Game.GetTransactionSystem():GetItemInSlot(owner,TweakDBID.new("AttachmentSlots." .. slot))
      if attachment then
        if slot ~= "Feet" then found = true end
        collect(attachment, slot == "Feet")
        if slot == "Legs" or slot == "UnderwearBottom" then lower = true end
      end
    end
    return found
  end
  local player = Game.GetPlayer()
  -- Inventory previews may show a different outfit from the player. Their
  -- rendered components are authoritative, including when completely naked.
  if player == entity then attachments(entity) end
  if lower and not item.compactPelvis then hidden.pelvis = true end
  if item.intactSkin then
    for region in pairs(hidden) do if not region:match("^[lr]leg_%d$") then hidden[region]=nil end end
  end
  -- The compact pubic cap must not hang below the crotch of trousers.
  -- Keep the adjacent hips, legs and exposed lower back fully visible.
  if lower then hidden.pelvis = true end
  local state = {model=item.id, lower_clothing=lower, hidden_regions=hidden, garments=rows, parts={}}
  for _, target in ipairs({{part=head and "body" or "body_fpp", comp=body},{part="arms_fpp",comp=arms}}) do
    local masks = item.coverage[target.part]
    if masks and target.comp then
      local mask = masks.full
      for _, zone in ipairs(masks.zones or {}) do
        if hidden[zone.region] then mask = mask-zone.bit end
      end
      setMask(target.comp,mask)
      state.parts[target.part] = tostring(target.comp:ABGetMask())
    end
  end
  state.components={}
  for _,comp in ipairs(own) do
    local mesh=hash(comp:ABGetMeshHash())
    if mesh~="0" then state.components[#state.components+1]={name=NameToString(comp:ABGetName()),hash=mesh,mask=tostring(comp:ABGetMask()),enabled=comp:ABIsEnabled()} end
  end
  persist(head and "coverage-paperdoll.json" or "coverage-player.json", state)
end
local function updateFace(entity)
  if not entity or not item.face then return end
  local head
  for _, comp in ipairs(components(entity)) do
    if NameToString(comp:ABGetName()) == item.head then head = comp; break end
  end
  if not head then return end
  local choice = item.face.expressions[1]
  for _, candidate in ipairs(item.face.expressions) do
    if candidate.id == expression then choice = candidate end
  end
  if not choice then return end
  local state = 1
  if item.face.blink and #choice.states == 3 then
    local phase = faceClock%4.3
    if phase < .06 or (phase >= .14 and phase < .20) then state = 2
    elseif phase < .14 then state = 3 end
  end
  local path = choice.states[state]
  if path then
    local desired = ResRef.FromString(path)
    if hash(head:ABGetMeshHash()) ~= hash(ResRef.GetHash(desired)) then
      if not head:ABChangeMesh(desired) then warn("Espressione non applicata: " .. path) end
      persist("face-state.json", {model=item.id, expression=choice.id, blink_state=state, mesh=path})
    end
  end
end
registerForEvent("onInit", function()
  ObserveAfter("gameuiInGameMenuGameController", "OnPuppetReady", function(_, scene, puppet)
    if puppet then previews[tostring(scene)] = puppet end
  end)
  print("Avatar Bridge: compatibilita 2.5, " .. item.name)
end)
registerForEvent("onOverlayOpen", function() visible=true end)
registerForEvent("onOverlayClose", function() visible=false end)
registerForEvent("onDraw", function()
  if not visible then return end
  if ImGui.Begin("Avatar") then
    ImGui.TextWrapped(item.name)
    ImGui.TextWrapped("Compatibilita 2.5: per cambiare avatar usa l'app a gioco chiuso.")
    if item.face then
      for _, choice in ipairs(item.face.expressions) do
        if ImGui.RadioButton(choice.name .. "##face", expression == choice.id) then expression=choice.id end
      end
    end
  end
  ImGui.End()
end)
registerForEvent("onUpdate", function(delta)
  faceClock=faceClock+delta; faceElapsed=faceElapsed+delta
  if faceElapsed >= .03 then
    faceElapsed=0
    local success, message=pcall(function() updateFace(Game.GetPlayer()) end)
    if not success then warn(message) end
    for _, puppet in pairs(previews) do
      local ok, errorMessage=pcall(function() updateFace(puppet) end)
      if not ok then warn(errorMessage) end
    end
  end
  elapsed = elapsed+delta
  if elapsed < .5 then return end
  elapsed = 0
  local ok, message = pcall(function() apply(Game.GetPlayer()) end)
  if not ok then warn(message) end
  for _, puppet in pairs(previews) do
    local success, errorMessage = pcall(function() apply(puppet) end)
    if not success then warn(errorMessage) end
  end
end)
