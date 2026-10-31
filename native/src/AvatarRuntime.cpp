#include "Red/TypeInfo/Definition.hpp"

// Deliberately private names: these APIs do not replace Codeware's methods.
struct AvatarEntity : Red::Entity
{
    Red::DynArray<Red::Handle<Red::IComponent>> ABGetComponents() { return components; }
};
RTTI_EXPAND_CLASS(Red::Entity, AvatarEntity, { RTTI_METHOD(ABGetComponents); });

struct AvatarComponent : Red::IComponent
{
    bool Ready()
    {
        // Components exist while their entity is still assembling. Refreshing
        // an appearance then can queue a render command with a null proxy.
        if (!owner || static_cast<uint8_t>(owner->status) != 4) return false;
        if (GetType()->IsA(Red::GetClass<Red::entSkinnedMeshComponent>()))
        {
            auto mesh = reinterpret_cast<Red::entSkinnedMeshComponent*>(this);
            return mesh->meshHandle && mesh->renderProxy;
        }
        if (GetType()->IsA(Red::GetClass<Red::entMorphTargetSkinnedMeshComponent>()))
        {
            auto mesh = reinterpret_cast<Red::entMorphTargetSkinnedMeshComponent*>(this);
            return mesh->meshHandle && mesh->renderProxy;
        }
        if (GetType()->IsA(Red::GetClass<Red::entMeshComponent>()))
        {
            auto& loaded = *reinterpret_cast<Red::Handle<Red::CMesh>*>(reinterpret_cast<uint8_t*>(this) + 0x158);
            return static_cast<bool>(loaded);
        }
        return false;
    }
    Red::CName ABGetName() { return name; }
    bool ABIsEnabled() { return isEnabled; }
    uint64_t ABGetMeshHash()
    {
        if (GetType()->IsA(Red::GetClass<Red::entSkinnedMeshComponent>()))
        {
            auto mesh = reinterpret_cast<Red::entSkinnedMeshComponent*>(this);
            return mesh->meshHandle ? mesh->meshHandle->path.hash : mesh->mesh.path.hash;
        }
        if (GetType()->IsA(Red::GetClass<Red::entMeshComponent>()))
        {
            auto& loaded = *reinterpret_cast<Red::Handle<Red::CMesh>*>(reinterpret_cast<uint8_t*>(this) + 0x158);
            return loaded ? loaded->path.hash : reinterpret_cast<Red::entMeshComponent*>(this)->mesh.path.hash;
        }
        return 0;
    }
    uint64_t* Mask()
    {
        if (GetType()->IsA(Red::GetClass<Red::entSkinnedMeshComponent>()))
            return &reinterpret_cast<Red::entSkinnedMeshComponent*>(this)->chunkMask;
        if (GetType()->IsA(Red::GetClass<Red::entMorphTargetSkinnedMeshComponent>()))
            return &reinterpret_cast<Red::entMorphTargetSkinnedMeshComponent*>(this)->chunkMask;
        if (GetType()->IsA(Red::GetClass<Red::entMeshComponent>()))
            return &reinterpret_cast<Red::entMeshComponent*>(this)->chunkMask;
        return nullptr;
    }
    uint64_t ABGetMask() { auto mask = Mask(); return mask ? *mask : 0; }
    bool ABSetMask(uint64_t value)
    {
        auto mask = Mask();
        if (!mask || !Ready()) return false;
        if (*mask == value) return true;
        *mask = value;
        Core::RawVFunc<0x280, void(Red::IComponent::*)()>()(this);
        return true;
    }
    bool ABChangeMesh(const Red::ResourceAsyncReference<>& resource)
    {
        if (!Ready() || !Mask() || !resource.path || GetType()->IsA(Red::GetClass<Red::entMorphTargetSkinnedMeshComponent>())) return false;
        if (GetType()->IsA(Red::GetClass<Red::entSkinnedMeshComponent>()))
        {
            auto mesh = reinterpret_cast<Red::entSkinnedMeshComponent*>(this);
            // Wait for the preceding asynchronous load instead of submitting
            // the same facial replacement every update while it is pending.
            if (mesh->meshHandle->path.hash != mesh->mesh.path.hash) return false;
            mesh->mesh = resource.path;
        }
        else
            reinterpret_cast<Red::entMeshComponent*>(this)->mesh = resource.path;
        Red::JobQueue queue;
        Core::RawVFunc<0x260, uint64_t(Red::IComponent::*)(Red::JobQueue&)>()(this, queue);
        queue.Dispatch([weak = Red::AsWeakHandle(this)] {
            if (auto component = weak.Lock(); component && reinterpret_cast<AvatarComponent*>(component.instance)->Ready())
                Core::RawVFunc<0x280, void(Red::IComponent::*)()>()(component.instance);
        });
        return true;
    }
};
RTTI_EXPAND_CLASS(Red::IComponent, AvatarComponent, {
    RTTI_METHOD(ABGetName);
    RTTI_METHOD(ABIsEnabled);
    RTTI_METHOD(ABGetMeshHash);
    RTTI_METHOD(ABGetMask);
    RTTI_METHOD(ABSetMask);
    RTTI_METHOD(ABChangeMesh);
});
