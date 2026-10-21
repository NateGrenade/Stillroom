import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// A discoverable local transfer, not an approved import. The existing preview
/// still validates the full manifest, hashes and available storage before import.
public struct PocketReceivedLibrary: Identifiable, Equatable, Sendable {
    public let url: URL
    public let filename: String
    public let byteCount: Int64
    public let receivedAt: Date
    public var id: String { filename }
}

public enum PocketReceivedLibraries {
    public static let maximumFiles = 50
    public static let maximumEntries = 2048

    /// Reads only Documents' direct children and 12 bytes per candidate. Desktop
    /// transfer writes a hidden .partial and atomically publishes its final name.
    /// Never create, move, remove or import anything during discovery.
    public static func discover(in documents: URL) throws -> [PocketReceivedLibrary] {
        guard documents.isFileURL else { throw PocketTransferError.invalid("The local transfer folder is unavailable.") }
        let descriptor = open(documents.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        guard let directory = fdopendir(descriptor) else {
            let error = errno; close(descriptor)
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(error))
        }
        defer { closedir(directory) }
        var libraries = [PocketReceivedLibrary]()
        var visited = 0
        while visited < maximumEntries {
            errno = 0
            guard let entry = readdir(directory) else {
                if errno != 0 { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
                break
            }
            visited += 1
            var nameBytes = entry.pointee.d_name
            let capacity = MemoryLayout.size(ofValue: nameBytes)
            let name = withUnsafePointer(to: &nameBytes) { pointer in
                pointer.withMemoryRebound(to: CChar.self, capacity: capacity) { String(validatingUTF8: $0) }
            }
            guard let name, !name.hasPrefix("."), !name.lowercased().contains(".partial"),
                  (name as NSString).pathExtension.lowercased() == "stillroomlibrary",
                  validText(name, maximum: 4096, required: true) else { continue }
            if let library = inspect(name: name, in: documents, directoryDescriptor: descriptor) { libraries.append(library) }
        }
        libraries.sort {
            if $0.receivedAt != $1.receivedAt { return $0.receivedAt > $1.receivedAt }
            return $0.filename < $1.filename
        }
        return Array(libraries.prefix(maximumFiles))
    }

    private static func inspect(name: String, in documents: URL, directoryDescriptor: Int32) -> PocketReceivedLibrary? {
        let descriptor = openat(directoryDescriptor, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        guard descriptor >= 0 else { return nil }
        defer { close(descriptor) }
        var info = stat()
        guard fstat(descriptor, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_nlink == 1,
              info.st_size >= 13, Int64(info.st_size) <= PocketLimits.totalBytes + Int64(PocketLimits.manifestBytes) + 12 else { return nil }
        var header = [UInt8](repeating: 0, count: 12)
        var count = 0
        while count < header.count {
            let readCount = header.withUnsafeMutableBytes { bytes in
                read(descriptor, bytes.baseAddress!.advanced(by: count), bytes.count - count)
            }
            if readCount < 0 && errno == EINTR { continue }
            guard readCount > 0 else { return nil }
            count += readCount
        }
        guard Data(header.prefix(8)) == PocketLimits.header else { return nil }
        let manifestBytes = header.suffix(4).reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        guard manifestBytes > 0, manifestBytes <= PocketLimits.manifestBytes,
              Int64(info.st_size) >= 12 + Int64(manifestBytes) else { return nil }
        var after = stat()
        guard fstat(descriptor, &after) == 0, after.st_size == info.st_size, after.st_nlink == 1 else { return nil }
        #if canImport(Darwin)
        guard after.st_mtimespec.tv_sec == info.st_mtimespec.tv_sec,
              after.st_mtimespec.tv_nsec == info.st_mtimespec.tv_nsec else { return nil }
        let receivedAt = Date(timeIntervalSince1970: Double(info.st_mtimespec.tv_sec) + Double(info.st_mtimespec.tv_nsec) / 1_000_000_000)
        #else
        guard after.st_mtim.tv_sec == info.st_mtim.tv_sec,
              after.st_mtim.tv_nsec == info.st_mtim.tv_nsec else { return nil }
        let receivedAt = Date(timeIntervalSince1970: Double(info.st_mtim.tv_sec) + Double(info.st_mtim.tv_nsec) / 1_000_000_000)
        #endif
        return .init(url: documents.appendingPathComponent(name), filename: name, byteCount: Int64(info.st_size), receivedAt: receivedAt)
    }
}
