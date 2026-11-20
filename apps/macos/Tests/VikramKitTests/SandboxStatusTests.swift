import Foundation
import Testing
import VikramKit

@Suite struct SandboxStatusTests {
    @Test func decodesSavedSandboxState() throws {
        let data = Data(#"{"sandbox":{"state":"saved"}}"#.utf8)
        let status = try JSONDecoder().decode(WorkbenchStatus.self, from: data)
        #expect(status.sandbox["state"]?.string == "saved")
    }

    @Test func olderSidecarsNeedNoSandboxField() throws {
        let status = try JSONDecoder().decode(WorkbenchStatus.self, from: Data("{}".utf8))
        #expect(status.sandbox.isEmpty)
    }
}
