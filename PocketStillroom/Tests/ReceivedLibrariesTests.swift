import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

private struct ReceivedTestFailure: Error, CustomStringConvertible { let description: String }
private func check(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
    if try !condition() { throw ReceivedTestFailure(description: message) }
}

@main struct ReceivedLibrariesTests {
    static let files = FileManager.default
    static var passed = 0
    static func test(_ name: String, _ work: (URL) throws -> Void) throws {
        let root = files.temporaryDirectory.appendingPathComponent("PocketReceived-" + UUID().uuidString, isDirectory: true)
        try files.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? files.removeItem(at: root) }
        try work(root); passed += 1; print("PASS \(name)")
    }
    static func fixture(_ name: String, in root: URL, date: TimeInterval = 1000, manifestLength: UInt32 = 2) throws -> URL {
        let url = root.appendingPathComponent(name)
        var bytes = Data("STLRMB01".utf8), length = manifestLength.bigEndian
        withUnsafeBytes(of: &length) { bytes.append(contentsOf: $0) }
        bytes.append(Data("{}".utf8))
        try bytes.write(to: url)
        try files.setAttributes([.modificationDate: Date(timeIntervalSince1970: date)], ofItemAtPath: url.path)
        return url
    }
    static func main() throws {
        try test("discovers only complete root files and leaves every byte in place") { root in
            let complete = try fixture("Stillroom-2026-10-21-" + UUID().uuidString + ".stillroomlibrary", in: root)
            let bytes = try Data(contentsOf: complete)
            let nested = root.appendingPathComponent("Transfers", isDirectory: true)
            try files.createDirectory(at: nested, withIntermediateDirectories: false)
            _ = try fixture("nested.stillroomlibrary", in: nested)
            let found = try PocketReceivedLibraries.discover(in: root)
            try check(found.count == 1 && found[0].url == complete, "Complete root transfer missing or nested content scanned")
            try check(found[0].filename == complete.lastPathComponent && found[0].id == complete.lastPathComponent, "Display filename/identity changed")
            try check(found[0].byteCount == bytes.count && found[0].receivedAt == Date(timeIntervalSince1970: 1000), "Size/date incorrect")
            try check(try Data(contentsOf: complete) == bytes, "Discovery altered transfer data")
            try check(try files.contentsOfDirectory(atPath: root.path).count == 2, "Discovery created or removed files")
        }
        try test("hidden, partial and wrong-extension files never appear") { root in
            for name in [".stillroomlibrary", "." + UUID().uuidString + ".partial", ".hidden.stillroomlibrary", "transfer.stillroomlibrary.partial", "transfer.partial.stillroomlibrary", "transfer.PARTIAL.stillroomlibrary", "transfer.zip", "transfer"] {
                _ = try fixture(name, in: root)
            }
            try check(try PocketReceivedLibraries.discover(in: root).isEmpty, "Hidden/partial/non-library item visible")
            let visible = try fixture("My exported backup.STILLROOMLIBRARY", in: root)
            try check(try PocketReceivedLibraries.discover(in: root).map(\.url) == [visible], "Valid extension case or arbitrary filename rejected")
        }
        try test("bad magic, short and impossible headers are rejected with bounded reads") { root in
            try Data("STLRMB00anything".utf8).write(to: root.appendingPathComponent("bad.stillroomlibrary"))
            try Data("STLRMB01".utf8).write(to: root.appendingPathComponent("short.stillroomlibrary"))
            _ = try fixture("zero.stillroomlibrary", in: root, manifestLength: 0)
            _ = try fixture("truncated.stillroomlibrary", in: root, manifestLength: 2048)
            _ = try fixture("oversized.stillroomlibrary", in: root, manifestLength: UInt32(8 * 1024 * 1024 + 1))
            try check(try PocketReceivedLibraries.discover(in: root).isEmpty, "Invalid header visible")
        }
        try test("symbolic links, hard links, directories and FIFOs are not opened as libraries") { root in
            let outside = root.appendingPathComponent("outside", isDirectory: true)
            try files.createDirectory(at: outside, withIntermediateDirectories: false)
            let source = try fixture("source.stillroomlibrary", in: outside)
            try files.createSymbolicLink(at: root.appendingPathComponent("link.stillroomlibrary"), withDestinationURL: source)
            try files.linkItem(at: source, to: root.appendingPathComponent("hardlink.stillroomlibrary"))
            try files.createDirectory(at: root.appendingPathComponent("folder.stillroomlibrary"), withIntermediateDirectories: false)
            try check(mkfifo(root.appendingPathComponent("pipe.stillroomlibrary").path, 0o600) == 0, "Could not create generated FIFO")
            try check(try PocketReceivedLibraries.discover(in: root).isEmpty, "Non-regular/link item visible")
        }
        try test("root symlinks and missing folders are refused without creation") { root in
            let real = root.appendingPathComponent("real", isDirectory: true)
            try files.createDirectory(at: real, withIntermediateDirectories: false)
            let link = root.appendingPathComponent("Documents", isDirectory: true)
            try files.createSymbolicLink(at: link, withDestinationURL: real)
            do { _ = try PocketReceivedLibraries.discover(in: link); throw ReceivedTestFailure(description: "Root symlink accepted") }
            catch is ReceivedTestFailure { throw ReceivedTestFailure(description: "Root symlink accepted") }
            catch {}
            let missing = root.appendingPathComponent("missing", isDirectory: true)
            do { _ = try PocketReceivedLibraries.discover(in: missing); throw ReceivedTestFailure(description: "Missing directory accepted") }
            catch is ReceivedTestFailure { throw ReceivedTestFailure(description: "Missing directory accepted") }
            catch {}
            try check(!files.fileExists(atPath: missing.path), "Discovery created a missing Documents folder")
        }
        try test("newest first and deterministic filename tie-break keep collision names distinct") { root in
            _ = try fixture("Stillroom-same.stillroomlibrary", in: root, date: 1000)
            _ = try fixture("Stillroom-same (1).stillroomlibrary", in: root, date: 2000)
            _ = try fixture("Stillroom-same (2).stillroomlibrary", in: root, date: 2000)
            let found = try PocketReceivedLibraries.discover(in: root)
            try check(found.map(\.filename) == ["Stillroom-same (1).stillroomlibrary", "Stillroom-same (2).stillroomlibrary", "Stillroom-same.stillroomlibrary"], "Wrong order or collision names lost")
            try check(Set(found.map(\.id)).count == 3, "Duplicate list identifiers")
        }
        try test("atomic final rename makes a hidden transfer visible only after publication") { root in
            let partial = try fixture("." + UUID().uuidString + ".partial", in: root)
            try check(try PocketReceivedLibraries.discover(in: root).isEmpty, "Partial transfer visible")
            let complete = root.appendingPathComponent("Stillroom-2026-10-21-" + UUID().uuidString + ".stillroomlibrary")
            try files.moveItem(at: partial, to: complete)
            try check(try PocketReceivedLibraries.discover(in: root).map(\.url) == [complete], "Completed rename not discovered")
        }
        try test("result and directory scan stay bounded with disposable collisions") { root in
            for index in 0..<(PocketReceivedLibraries.maximumEntries + 10) {
                _ = try fixture(String(format: "Stillroom-%04d.stillroomlibrary", index), in: root, date: Double(index))
            }
            let found = try PocketReceivedLibraries.discover(in: root)
            try check(found.count == PocketReceivedLibraries.maximumFiles, "Bounded result length wrong")
            try check(zip(found, found.dropFirst()).allSatisfy { $0.receivedAt >= $1.receivedAt }, "Bounded result not newest first")
            try check(try files.contentsOfDirectory(atPath: root.path).count == PocketReceivedLibraries.maximumEntries + 10, "Bounded discovery deleted files")
        }
        print("Received-library Mac-host checks: \(passed) passed. No simulator, app build or device validation performed.")
    }
}
