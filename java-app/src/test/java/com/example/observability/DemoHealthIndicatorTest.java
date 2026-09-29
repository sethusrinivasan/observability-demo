package com.example.observability;

import org.junit.jupiter.api.Test;
import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.Status;

import static org.junit.jupiter.api.Assertions.assertEquals;

class DemoHealthIndicatorTest {
    @Test
    void reportsUpWithSpringBootDetails() {
        Health health = new DemoHealthIndicator().health();

        assertEquals(Status.UP, health.getStatus());
        assertEquals("spring-boot", health.getDetails().get("framework"));
        assertEquals("java", health.getDetails().get("language"));
        assertEquals("actuator", health.getDetails().get("sample"));
    }
}
