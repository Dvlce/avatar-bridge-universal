#include "Application.hpp"
#include "App/Project.hpp"
#include "Core/Foundation/RuntimeProvider.hpp"
#include "Support/MinHook/MinHookProvider.hpp"
#include "Support/RED4ext/RED4extProvider.hpp"
#include "Support/RedLib/RedLibProvider.hpp"
#include "Support/Spdlog/SpdlogProvider.hpp"
App::Application::Application(HMODULE module, const RED4ext::v1::Sdk* sdk)
{
    Register<Core::RuntimeProvider>(module)->SetBaseImagePathDepth(2);
    Register<Support::MinHookProvider>();
    Register<Support::SpdlogProvider>();
    Register<Support::RED4extProvider>(module, sdk)->EnableAddressLibrary();
    Register<Support::RedLibProvider>();
}
void App::Application::OnStarting() { LogInfo("Avatar Bridge 2.5 runtime initializing"); }
void App::Application::OnStarted() { LogInfo("Avatar Bridge 2.5 runtime ready"); }
