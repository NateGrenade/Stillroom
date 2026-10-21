"""Generate a dependency-free Xcode app project; no Xcode/signing changes."""
from pathlib import Path
import hashlib
import plistlib

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / 'Project' / 'PocketStillroom.xcodeproj'


def identifier(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24].upper()


def quoted(value):
    assert '\n' not in value and '"' not in value
    return '"' + value + '"'


def main():
    sources = sorted(path for folder in ('Sources', 'App') for path in (ROOT / folder).rglob('*.swift'))
    assert sources
    files = []
    build_files = []
    references = []
    source_ids = []
    resource_ids = []
    for path in sources:
        relative = '../' + str(path.relative_to(ROOT))
        ref = identifier('reference:' + relative)
        build = identifier('build:' + relative)
        references.append(ref)
        source_ids.append(build)
        files.append(f'{ref} = {{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {quoted(relative)}; sourceTree = SOURCE_ROOT; }};')
        build_files.append(f'{build} = {{isa = PBXBuildFile; fileRef = {ref}; }};')
    for path in sorted((ROOT / 'Help').iterdir()):
        if path.suffix not in ('.md', '.json'):
            continue
        relative = '../' + str(path.relative_to(ROOT))
        ref = identifier('reference:' + relative)
        build = identifier('build:' + relative)
        references.append(ref)
        resource_ids.append(build)
        files.append(f'{ref} = {{isa = PBXFileReference; lastKnownFileType = text; path = {quoted(relative)}; sourceTree = SOURCE_ROOT; }};')
        build_files.append(f'{build} = {{isa = PBXBuildFile; fileRef = {ref}; }};')
    product, main_group, products = map(identifier, ('product', 'group', 'products'))
    sources_phase, resources_phase, frameworks_phase = map(identifier, ('sources', 'resources', 'frameworks'))
    target, project, target_list, project_list = map(identifier, ('target', 'project', 'target_list', 'project_list'))
    target_debug, target_release, project_debug, project_release = map(identifier, ('target_debug', 'target_release', 'project_debug', 'project_release'))
    test_target, test_product, test_phase, test_frameworks, test_list, test_debug, test_release, test_proxy, test_dependency = map(identifier, ('test_target', 'test_product', 'test_phase', 'test_frameworks', 'test_list', 'test_debug', 'test_release', 'test_proxy', 'test_dependency'))
    test_builds = []
    for test_source in sorted((ROOT / 'Tests').glob('Pocket*Tests.swift')):
        test_path = '../' + str(test_source.relative_to(ROOT))
        test_ref, test_build = identifier('reference:' + test_path), identifier('build:' + test_path)
        references.append(test_ref)
        test_builds.append(test_build)
        files.append(f'{test_ref} = {{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {quoted(test_path)}; sourceTree = SOURCE_ROOT; }};')
        build_files.append(f'{test_build} = {{isa = PBXBuildFile; fileRef = {test_ref}; }};')
    assert test_builds, 'At least one simulator test source is required'
    objects = files + build_files + [
        f'{product} = {{isa = PBXFileReference; explicitFileType = wrapper.application; path = "Pocket Stillroom Dev.app"; sourceTree = BUILT_PRODUCTS_DIR; }};',
        f'{test_product} = {{isa = PBXFileReference; explicitFileType = wrapper.cfbundle; path = PocketSimulatorTests.xctest; sourceTree = BUILT_PRODUCTS_DIR; }};',
        f'{main_group} = {{isa = PBXGroup; children = ({",".join(references + [products])}); sourceTree = "<group>"; }};',
        f'{products} = {{isa = PBXGroup; children = ({product},{test_product}); name = Products; sourceTree = "<group>"; }};',
        f'{sources_phase} = {{isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = ({",".join(source_ids)}); runOnlyForDeploymentPostprocessing = 0; }};',
        f'{resources_phase} = {{isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = ({",".join(resource_ids)}); runOnlyForDeploymentPostprocessing = 0; }};',
        f'{frameworks_phase} = {{isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0; }};',
        f'{target} = {{isa = PBXNativeTarget; buildConfigurationList = {target_list}; buildPhases = ({sources_phase},{frameworks_phase},{resources_phase}); buildRules = (); dependencies = (); name = PocketStillroom; productName = "Pocket Stillroom Dev"; productReference = {product}; productType = "com.apple.product-type.application"; }};',
        f'{test_phase} = {{isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = ({",".join(test_builds)}); runOnlyForDeploymentPostprocessing = 0; }};',
        f'{test_frameworks} = {{isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0; }};',
        f'{test_proxy} = {{isa = PBXContainerItemProxy; containerPortal = {project}; proxyType = 1; remoteGlobalIDString = {target}; remoteInfo = PocketStillroom; }};',
        f'{test_dependency} = {{isa = PBXTargetDependency; target = {target}; targetProxy = {test_proxy}; }};',
        f'{test_target} = {{isa = PBXNativeTarget; buildConfigurationList = {test_list}; buildPhases = ({test_phase},{test_frameworks}); buildRules = (); dependencies = ({test_dependency}); name = PocketSimulatorTests; productName = PocketSimulatorTests; productReference = {test_product}; productType = "com.apple.product-type.bundle.unit-test"; }};',
        f'{project} = {{isa = PBXProject; attributes = {{BuildIndependentTargetsInParallel = YES; LastUpgradeCheck = 1620; }}; buildConfigurationList = {project_list}; compatibilityVersion = "Xcode 14.0"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en,Base); mainGroup = {main_group}; productRefGroup = {products}; projectDirPath = ""; projectRoot = ""; targets = ({target},{test_target}); }};',
    ]
    target_settings = 'CODE_SIGN_STYLE = Automatic; DEVELOPMENT_TEAM = ""; GENERATE_INFOPLIST_FILE = NO; INFOPLIST_FILE = ../App/Info.plist; IPHONEOS_DEPLOYMENT_TARGET = 17.0; PRODUCT_BUNDLE_IDENTIFIER = com.nategrenade.stillroom.pocket.dev; PRODUCT_NAME = "Pocket Stillroom Dev"; SDKROOT = iphoneos; SUPPORTED_PLATFORMS = "iphoneos iphonesimulator"; SUPPORTS_MACCATALYST = NO; SWIFT_VERSION = 5.0; TARGETED_DEVICE_FAMILY = "1,2"; ENABLE_USER_SCRIPT_SANDBOXING = YES;'
    target_settings += ' PRODUCT_MODULE_NAME = PocketStillroom;'
    project_settings = 'CLANG_ENABLE_MODULES = YES; SWIFT_STRICT_CONCURRENCY = complete;'
    test_settings = 'CODE_SIGN_STYLE = Automatic; DEVELOPMENT_TEAM = ""; GENERATE_INFOPLIST_FILE = YES; IPHONEOS_DEPLOYMENT_TARGET = 17.0; PRODUCT_BUNDLE_IDENTIFIER = com.nategrenade.stillroom.pocket.dev.tests; PRODUCT_NAME = PocketSimulatorTests; SDKROOT = iphoneos; SUPPORTED_PLATFORMS = "iphoneos iphonesimulator"; SWIFT_VERSION = 5.0; TARGETED_DEVICE_FAMILY = "1,2"; TEST_HOST = "$(BUILT_PRODUCTS_DIR)/Pocket Stillroom Dev.app/Pocket Stillroom Dev"; BUNDLE_LOADER = "$(TEST_HOST)"; TEST_TARGET_NAME = PocketStillroom; SKIP_INSTALL = YES;'
    for item, label, settings in ((target_debug, 'Debug', target_settings + ' ENABLE_TESTABILITY = YES; SWIFT_OPTIMIZATION_LEVEL = "-Onone"; DEBUG_INFORMATION_FORMAT = dwarf;'), (target_release, 'Release', target_settings + ' SWIFT_OPTIMIZATION_LEVEL = "-O";'), (project_debug, 'Debug', project_settings), (project_release, 'Release', project_settings)):
        objects.append(f'{item} = {{isa = XCBuildConfiguration; buildSettings = {{{settings}}}; name = {label}; }};')
    for item, label in ((test_debug, 'Debug'), (test_release, 'Release')):
        objects.append(f'{item} = {{isa = XCBuildConfiguration; buildSettings = {{{test_settings}}}; name = {label}; }};')
    for item, configs in ((target_list, (target_debug, target_release)), (project_list, (project_debug, project_release)), (test_list, (test_debug, test_release))):
        objects.append(f'{item} = {{isa = XCConfigurationList; buildConfigurations = ({",".join(configs)}); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release; }};')
    payload = '// !$*UTF8*$!\n{archiveVersion = 1; classes = {}; objectVersion = 56; objects = {\n' + '\n'.join(objects) + f'\n}}; rootObject = {project}; }}\n'
    PROJECT.mkdir(exist_ok=True)
    (PROJECT / 'project.pbxproj').write_text(payload)
    scheme = PROJECT / 'xcshareddata' / 'xcschemes'
    scheme.mkdir(parents=True, exist_ok=True)
    reference = f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{target}" BuildableName="Pocket Stillroom Dev.app" BlueprintName="PocketStillroom" ReferencedContainer="container:PocketStillroom.xcodeproj"/>'
    (scheme / 'PocketStillroom.xcscheme').write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="1620" version="1.3"><BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">{reference}</BuildActionEntry></BuildActionEntries></BuildAction><LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" debugServiceExtension="internal" allowLocationSimulation="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference}</BuildableProductRunnable></LaunchAction><ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference}</BuildableProductRunnable></ProfileAction><AnalyzeAction buildConfiguration="Debug"/><ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/></Scheme>
''')
    scheme_path = scheme / 'PocketStillroom.xcscheme'
    test_reference = f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{test_target}" BuildableName="PocketSimulatorTests.xctest" BlueprintName="PocketSimulatorTests" ReferencedContainer="container:PocketStillroom.xcodeproj"/>'
    test_action = f'<TestAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" shouldUseLaunchSchemeArgsEnv="YES"><Testables><TestableReference skipped="NO">{test_reference}</TestableReference></Testables><MacroExpansion>{reference}</MacroExpansion></TestAction>'
    scheme_text = scheme_path.read_text().replace('</BuildAction><LaunchAction', '</BuildAction>' + test_action + '<LaunchAction')
    scheme_path.write_text(scheme_text)
    with (ROOT / 'App' / 'Info.plist').open('rb') as handle:
        assert plistlib.load(handle)['CFBundleIdentifier'] == '$(PRODUCT_BUNDLE_IDENTIFIER)'
    print(f'Generated Xcode project with {len(sources)} Swift source files; no build/sign/install performed.')


if __name__ == '__main__':
    main()
