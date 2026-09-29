package com.example.observability;

import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.stereotype.Component;

/**
 * Custom Actuator health contributor. Included in the readiness group as {@code demo}.
 * Liveness stays on Spring's liveness state so a database blip does not restart the process.
 */
@Component
public class DemoHealthIndicator implements HealthIndicator {
    @Override
    public Health health() {
        return Health.up()
                .withDetail("framework", "spring-boot")
                .withDetail("language", "java")
                .withDetail("sample", "actuator")
                .build();
    }
}
